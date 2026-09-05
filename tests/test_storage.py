from __future__ import annotations

from webhook_replay.storage import Storage, WebhookRecord


def _add(store: Storage, method="POST", path="/hook", body=b"{}") -> int:
    return store.add(
        method=method,
        path=path,
        headers=[("Content-Type", "application/json")],
        body=body,
        remote_addr="127.0.0.1",
    )


def test_add_and_get_roundtrip(store):
    rid = _add(store, body=b'{"event": "ping"}')
    record = store.get(rid)
    assert isinstance(record, WebhookRecord)
    assert record.method == "POST"
    assert record.path == "/hook"
    assert record.body == b'{"event": "ping"}'
    assert record.content_type == "application/json"
    assert record.body_text() == '{"event": "ping"}'


def test_get_missing_returns_none(store):
    assert store.get(999) is None


def test_binary_body_preserved(store):
    payload = bytes(range(256))
    rid = _add(store, body=payload)
    record = store.get(rid)
    assert record.body == payload
    assert record.body_text() is None  # not valid UTF-8


def test_count_and_clear(store):
    for _ in range(3):
        _add(store)
    assert store.count() == 3
    removed = store.clear()
    assert removed == 3
    assert store.count() == 0


def test_ids_reset_after_clear(store):
    _add(store)
    store.clear()
    new_id = _add(store)
    assert new_id == 1  # AUTOINCREMENT sequence reset


def test_list_filter_by_method(store):
    _add(store, method="GET")
    _add(store, method="POST")
    _add(store, method="POST")
    posts = store.list(method="post")
    assert len(posts) == 2
    assert all(r.method == "POST" for r in posts)


def test_list_filter_by_path_substring(store):
    _add(store, path="/stripe/webhook")
    _add(store, path="/github/webhook")
    _add(store, path="/health")
    hits = store.list(path_contains="webhook")
    assert {r.path for r in hits} == {"/stripe/webhook", "/github/webhook"}


def test_list_path_filter_treats_like_wildcards_literally(store):
    _add(store, path="/a_b")
    _add(store, path="/ab")
    _add(store, path="/100%")
    assert {r.path for r in store.list(path_contains="_")} == {"/a_b"}
    assert {r.path for r in store.list(path_contains="%")} == {"/100%"}
    assert {r.path for r in store.list(path_contains="a")} == {"/a_b", "/ab"}
    assert store.list(path_contains="\\") == []


def test_list_newest_first_and_limit(store):
    ids = [_add(store) for _ in range(5)]
    recent = store.list(limit=2)
    assert [r.id for r in recent] == [ids[-1], ids[-2]]


def test_latest(store):
    _add(store)
    last_id = _add(store)
    assert store.latest(1)[0].id == last_id


def test_since_filter(store):
    common = dict(
        method="POST", headers=[("Content-Type", "application/json")], body=b"{}",
        remote_addr="127.0.0.1",
    )
    store.add(path="/old", received_at="2020-01-01T00:00:00.000+00:00", **common)
    store.add(path="/new", received_at="2030-01-01T00:00:00.000+00:00", **common)
    hits = store.list(since="2025-01-01T00:00:00.000+00:00")
    assert [r.path for r in hits] == ["/new"]


def test_prune_to_keeps_the_newest(store):
    ids = [_add(store, path=f"/h{i}") for i in range(5)]
    removed = store.prune_to(2)
    assert removed == 3
    assert [r.id for r in store.list()] == [ids[-1], ids[-2]]


def test_prune_to_is_a_noop_when_under_the_cap(store):
    _add(store)
    assert store.prune_to(10) == 0
    assert store.count() == 1


def test_prune_to_zero_means_unlimited(store):
    _add(store)
    assert store.prune_to(0) == 0
    assert store.count() == 1


def test_prune_older_than(store):
    common = dict(
        method="POST", headers=[], body=b"{}", remote_addr="127.0.0.1",
    )
    store.add(path="/old", received_at="2020-01-01T00:00:00.000+00:00", **common)
    store.add(path="/new", received_at="2030-01-01T00:00:00.000+00:00", **common)
    removed = store.prune_older_than("2025-01-01T00:00:00.000+00:00")
    assert removed == 1
    assert [r.path for r in store.list()] == ["/new"]
