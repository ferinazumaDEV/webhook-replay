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
    # X-Signature is masked by default; see test_to_curl_show_secrets.
    assert "-H 'X-Signature: <redacted>'" in curl
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


# --------------------------------------------------------------------------- #
# redaction on export
# --------------------------------------------------------------------------- #
def _secretive(store: Storage) -> int:
    return store.add(
        method="POST",
        path="/stripe",
        headers=[
            ("Content-Type", "application/json"),
            ("Authorization", "Bearer EXAMPLE-NOT-A-REAL-TOKEN"),
            ("Stripe-Signature", "t=1699,v1=abc123"),
            ("Cookie", "session=deadbeef"),
            ("X-Api-Key", "ak_9999"),
            ("X-Request-Id", "req_42"),
        ],
        body=b'{"event": "ping"}',
        remote_addr="127.0.0.1",
    )


def test_to_curl_masks_sensitive_headers_by_default(store):
    curl = to_curl(store.get(_secretive(store)))

    # None of the secret material reaches the output.
    for secret in ("EXAMPLE-NOT-A-REAL-TOKEN", "abc123", "deadbeef", "ak_9999"):
        assert secret not in curl

    # The header names survive, so the command still documents the request.
    assert "-H 'Authorization: Bearer <redacted>'" in curl
    assert "-H 'Stripe-Signature: <redacted>'" in curl
    assert "-H 'Cookie: <redacted>'" in curl
    assert "-H 'X-Api-Key: <redacted>'" in curl

    # Non-sensitive headers are untouched.
    assert "-H 'Content-Type: application/json'" in curl
    assert "-H 'X-Request-Id: req_42'" in curl


def test_to_curl_show_secrets_restores_real_values(store):
    curl = to_curl(store.get(_secretive(store)), show_secrets=True)

    assert "-H 'Authorization: Bearer EXAMPLE-NOT-A-REAL-TOKEN'" in curl
    assert "-H 'Stripe-Signature: t=1699,v1=abc123'" in curl
    assert "-H 'Cookie: session=deadbeef'" in curl
    assert "-H 'X-Api-Key: ak_9999'" in curl
    assert "<redacted>" not in curl


def test_to_curl_redaction_does_not_touch_the_body(store):
    # Redaction is a header concern only: the body is what you are debugging.
    rid = store.add(
        method="POST", path="/hook",
        headers=[("Authorization", "Bearer tok")],
        body=b'{"token": "in-the-body"}', remote_addr="127.0.0.1",
    )
    curl = to_curl(store.get(rid))
    assert "--data-binary '{\"token\": \"in-the-body\"}'" in curl
