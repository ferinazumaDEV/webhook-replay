"""Export a captured request as a runnable ``curl`` command."""
from __future__ import annotations

from shlex import quote

from .replay import build_target_url
from .storage import WebhookRecord

# Headers curl sets itself; keeping them would only add noise / conflicts.
_SKIP_HEADERS = {"host", "content-length", "connection"}


def to_curl(record: WebhookRecord, base_url: str = "http://localhost:8000") -> str:
    """Render ``record`` as a multi-line, copy-pasteable curl command."""
    url = build_target_url(base_url, record.path)
    parts: list[str] = [f"curl -X {record.method} {quote(url)}"]

    for key, value in record.headers:
        if key.lower() in _SKIP_HEADERS:
            continue
        parts.append(f"-H {quote(f'{key}: {value}')}")

    if record.body:
        text = record.body_text()
        if text is not None:
            parts.append(f"--data-binary {quote(text)}")
        else:
            # Binary body: curl can't take it inline safely, so flag it.
            parts.insert(
                0, f"# note: original body was {len(record.body)} bytes of binary data"
            )
            parts.append("--data-binary @/path/to/body.bin")

    return " \\\n  ".join(parts)
