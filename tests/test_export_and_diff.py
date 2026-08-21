from __future__ import annotations

from webhook_replay import _color
from webhook_replay.diff import diff_records
from webhook_replay.export import to_curl
from webhook_replay.storage import Storage

# Diffs/curl are asserted on plain text, so turn coloring off for these tests.
_color.set_enabled(False)


def _rec(store: Storage, body: bytes, path="/hook") -> int:
    return store.add(
        method="POST",
        path=path,
        headers=[("Content-Type", "application/json"), ("X-Signature", "sig-1")],
        body=body,
        remote_addr="127.0.0.1",
    )


def test_to_curl_basic_shape(store):
    record = store.get(_rec(store, b'{"event": "ping"}', path="/stripe?x=1"))
    curl = to_curl(record, base_url="http://localhost:9000")
    assert curl.startswith("curl -X POST 'http://localhost:9000/stripe?x=1'")
    assert "-H 'Content-Type: application/json'" in curl
    assert "-H 'X-Signature: sig-1'" in curl
    assert "--data-binary '{\"event\": \"ping\"}'" in curl


def test_to_curl_skips_host_and_content_length(store):
    rid = store.add(
        method="POST",
        path="/hook",
        headers=[("Host", "example.com"), ("Content-Length", "2")],
        body=b"{}",
        remote_addr="127.0.0.1",
    )
    curl = to_curl(store.get(rid))
    assert "Host" not in curl
    assert "Content-Length" not in curl


def test_to_curl_binary_body_flagged(store):
    record = store.get(_rec(store, bytes([0x00, 0xFF, 0x10])))
    curl = to_curl(record)
    assert "binary data" in curl
    assert "@/path/to/body.bin" in curl


def test_diff_json_ignores_key_order(store):
    a = store.get(_rec(store, b'{"a": 1, "b": 2}'))
    b = store.get(_rec(store, b'{"b": 2, "a": 1}'))
    out = diff_records(a, b)
    assert "identical" in out  # canonicalized JSON is equal


def test_diff_shows_value_change(store):
    a = store.get(_rec(store, b'{"amount": 100}'))
    b = store.get(_rec(store, b'{"amount": 250}'))
    out = diff_records(a, b)
    assert "-  \"amount\": 100" in out
    assert "+  \"amount\": 250" in out


def test_diff_plain_text(store):
    a = store.get(_rec(store, b"hello world"))
    b = store.get(_rec(store, b"hello there"))
    out = diff_records(a, b)
    assert "-hello world" in out
    assert "+hello there" in out
