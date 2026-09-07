#!/usr/bin/env python3
"""Does the installed webhook-replay actually work?

Run against a freshly installed wheel or sdist, never the repository.

Offline by design: it writes a capture to a temporary SQLite file, reads it
back, and checks that the redaction used by every output surface still hides a
credential. No network, no port binding, no fixtures.
"""
from __future__ import annotations

import importlib.metadata
import sys
import tempfile
from pathlib import Path

DISTRIBUTION = "webhook-replay"


def main() -> int:
    import webhook_replay
    from webhook_replay import Storage, is_sensitive, redact_headers

    installed = importlib.metadata.version(DISTRIBUTION)
    if webhook_replay.__version__ != installed:
        print(f"FAIL  __version__ is {webhook_replay.__version__} but metadata says {installed}")
        return 1
    print(f"ok    {DISTRIBUTION} {installed}")

    missing = [n for n in webhook_replay.__all__ if not hasattr(webhook_replay, n)]
    if missing:
        print(f"FAIL  __all__ advertises names that do not exist: {missing}")
        return 1
    print(f"ok    all {len(webhook_replay.__all__)} public names resolve")

    # The store is the product: a capture must survive a round trip.
    with tempfile.TemporaryDirectory() as tmp:
        store = Storage(Path(tmp) / "captures.db")
        store.add(method="POST", path="/hook", headers=[("X-Test", "1")],
                  body=b'{"n": 1}', remote_addr="127.0.0.1")
        if store.count() != 1:
            print(f"FAIL  stored one capture, count() says {store.count()}")
            return 1
    print("ok    a capture survives a write and read back")

    # Redaction is what stands between the store and a screen share.
    if not is_sensitive("Authorization"):
        print("FAIL  is_sensitive does not consider Authorization sensitive")
        return 1
    redacted = redact_headers([("Authorization", "Bearer hunter2"), ("Accept", "*/*")])
    if "hunter2" in str(redacted):
        print(f"FAIL  redact_headers leaked the credential: {redacted}")
        return 1
    if ("Accept", "*/*") not in redacted:
        print(f"FAIL  redact_headers mangled a harmless header: {redacted}")
        return 1
    print("ok    redact_headers hides a credential and leaves the rest alone")

    print("\nsmoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
