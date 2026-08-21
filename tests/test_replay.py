from __future__ import annotations

from webhook_replay.replay import build_target_url, replay
from webhook_replay.storage import Storage


def _store_request(store: Storage, **kw) -> int:
    defaults = dict(
        method="POST",
        path="/webhook?src=test",
        headers=[("Content-Type", "application/json"), ("X-Signature", "sig-1")],
        body=b'{"event": "ping"}',
        remote_addr="127.0.0.1",
    )
    defaults.update(kw)
    return store.add(**defaults)


def test_build_target_url_joins_cleanly():
    assert build_target_url("http://localhost:5000/", "/hook?x=1") == "http://localhost:5000/hook?x=1"
    assert build_target_url("http://localhost:5000", "/hook") == "http://localhost:5000/hook"


def test_replay_reaches_receiver_with_same_payload(store, receiver):
    rid = _store_request(store)
    record = store.get(rid)

    result = replay(record, receiver.base_url)

    assert result.ok
    assert result.status == 200
    assert len(receiver.requests) == 1

    got = receiver.requests[0]
    assert got["method"] == "POST"
    assert got["path"] == "/webhook?src=test"
    assert got["body"] == b'{"event": "ping"}'
    # Original custom header survives; hop-by-hop Host is not forwarded verbatim.
    assert got["headers"].get("X-Signature") == "sig-1"


def test_replay_multiple_times(store, receiver):
    record = store.get(_store_request(store))
    for _ in range(3):
        replay(record, receiver.base_url)
    assert len(receiver.requests) == 3


def test_replay_method_override(store, receiver):
    record = store.get(_store_request(store, method="POST"))
    result = replay(record, receiver.base_url, method="PUT")
    assert result.method == "PUT"
    assert receiver.requests[0]["method"] == "PUT"


def test_replay_extra_header(store, receiver):
    record = store.get(_store_request(store))
    replay(record, receiver.base_url, extra_headers=[("X-Debug", "yes")])
    assert receiver.requests[0]["headers"].get("X-Debug") == "yes"


def test_replay_non_2xx_is_captured_not_raised(store):
    # Receiver that always 404s.
    from tests.conftest import _RecordingReceiver

    rec = _RecordingReceiver()
    rec.start(status=404, body=b"nope")
    try:
        record = store.get(_store_request(store))
        result = replay(record, rec.base_url)
        assert result.status == 404
        assert result.ok is False
        assert result.body == b"nope"
    finally:
        rec.stop()


def test_replay_connection_error_reports_error(store):
    record = store.get(_store_request(store))
    # Nothing is listening on this port.
    result = replay(record, "http://127.0.0.1:1", timeout=2)
    assert result.error is not None
    assert result.ok is False
