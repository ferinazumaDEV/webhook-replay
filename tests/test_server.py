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
