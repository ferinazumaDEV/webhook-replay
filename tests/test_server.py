from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from webhook_replay.server import create_server


@pytest.fixture()
def capture_server(store):
    server = create_server(store, port=0, on_capture=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield store, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _post(url: str, data: bytes, headers: dict | None = None, method: str = "POST"):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read(), dict(resp.headers.items())


def test_captures_post_body_and_headers(capture_server):
    store, base = capture_server
    payload = json.dumps({"event": "invoice.paid", "amount": 4200}).encode()
    status, body, resp_headers = _post(
        base + "/stripe?test=1",
        payload,
        {"Content-Type": "application/json", "X-Signature": "abc123"},
    )

    assert status == 200
    assert json.loads(body) == {"received": True}
    assert "X-Webhook-Replay-Id" in resp_headers

    assert store.count() == 1
    record = store.get(1)
    assert record.method == "POST"
    assert record.path == "/stripe?test=1"
    assert record.body == payload
    assert record.header("X-Signature") == "abc123"


def test_captures_multiple_methods(capture_server):
    store, base = capture_server
    _post(base + "/a", b"", method="PUT")
    _post(base + "/b", b"x", method="DELETE")
    methods = {r.method for r in store.list()}
    assert methods == {"PUT", "DELETE"}


def test_custom_response_status(store):
    server = create_server(
        store, port=0, response_status=202, response_body=b"queued", on_capture=None
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        req = urllib.request.Request(f"http://{host}:{port}/x", data=b"{}", method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 202
            assert resp.read() == b"queued"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_concurrent_captures_are_all_persisted(capture_server):
    store, base = capture_server
    errors: list[Exception] = []

    def fire(i: int) -> None:
        try:
            _post(base + f"/hook/{i}", json.dumps({"n": i}).encode())
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(exc)

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert store.count() == 20


# --------------------------------------------------------------------------- #
# limits
# --------------------------------------------------------------------------- #
import contextlib  # noqa: E402
import socket  # noqa: E402
import urllib.error  # noqa: E402

from webhook_replay.server import (  # noqa: E402
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_CAPTURES,
)


@contextlib.contextmanager
def _serving(store, **kwargs):
    """Run a capture server with custom limits on an OS-assigned port."""
    kwargs.setdefault("on_capture", None)
    kwargs.setdefault("on_reject", None)
    server = create_server(store, port=0, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_defaults_are_bounded():
    # A capture server with no arguments is not an unbounded sink.
    assert DEFAULT_MAX_BODY_BYTES == 1024 * 1024
    assert DEFAULT_MAX_CAPTURES == 1000


def test_oversized_body_is_rejected_with_413(store):
    with _serving(store, max_body_bytes=1024) as base:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(base + "/big", b"x" * 2048)
        assert excinfo.value.code == 413
    # Rejected requests are not persisted.
    assert store.count() == 0


def test_body_at_the_limit_is_still_accepted(store):
    with _serving(store, max_body_bytes=1024) as base:
        status, _, _ = _post(base + "/ok", b"x" * 1024)
        assert status == 200
    assert store.count() == 1
    assert len(store.get(1).body) == 1024


def test_body_limit_can_be_disabled(store):
    with _serving(store, max_body_bytes=0) as base:
        status, _, _ = _post(base + "/ok", b"x" * 5000)
        assert status == 200
    assert store.count() == 1


def test_retention_limit_discards_the_oldest(store):
    with _serving(store, max_captures=3) as base:
        for i in range(6):
            _post(base + f"/hook/{i}", b"{}")

    assert store.count() == 3
    # The three most recent survive; the first three were evicted.
    assert sorted(r.path for r in store.list()) == ["/hook/3", "/hook/4", "/hook/5"]


def test_retention_limit_can_be_disabled(store):
    with _serving(store, max_captures=0) as base:
        for i in range(5):
            _post(base + f"/hook/{i}", b"{}")
    assert store.count() == 5


def test_read_timeout_drops_a_stalled_sender(store):
    # Announce a body, send nothing: the handler must give up, not hang.
    with _serving(store, read_timeout=0.5) as base:
        host, port = base.rsplit(":", 1)
        sock = socket.create_connection((host.rsplit("/", 1)[-1], int(port)), timeout=5)
        try:
            sock.sendall(
                b"POST /slow HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n"
            )
            sock.settimeout(5)
            # The server closes (or answers 408) well before our own timeout.
            reply = sock.recv(1024)
        finally:
            sock.close()
    assert reply == b"" or b"408" in reply
    assert store.count() == 0


def test_malformed_content_length_is_rejected(store):
    with _serving(store) as base:
        host, port = base.rsplit(":", 1)
        sock = socket.create_connection((host.rsplit("/", 1)[-1], int(port)), timeout=5)
        try:
            sock.sendall(
                b"POST /bad HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n"
            )
            sock.settimeout(5)
            reply = sock.recv(1024)
        finally:
            sock.close()
    assert b"400" in reply
    assert store.count() == 0
