from __future__ import annotations

import os
import sqlite3
import stat

import pytest

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


# --- a store that is not a store: text files, truncated files, file modes ------ #
#
# The CLI turns sqlite3.DatabaseError from Storage() into a one-line message and
# exit 2 (tests/test_cli.py). These pin what Storage() itself raises, so that the
# CLI's guard keeps catching the right thing.


def test_a_file_that_is_not_sqlite_is_a_database_error(tmp_path):
    path = tmp_path / "captures.db"
    path.write_text("hello, not a database\n", encoding="utf-8")
    with pytest.raises(sqlite3.DatabaseError):
        Storage(path)


def test_a_truncated_database_is_a_database_error(tmp_path):
    """A store cut short -- a copy that was interrupted, a disk that filled up --
    is detected on open, not on the first query that lands on a missing page:
    SQLite compares the page count in the header with the file size."""
    path = tmp_path / "captures.db"
    store = Storage(path)
    for i in range(50):
        _add(store, path=f"/hook/{i}", body=b"{}" * 300)
    with open(path, "r+b") as fh:
        fh.truncate(path.stat().st_size // 2)
    with pytest.raises(sqlite3.DatabaseError):
        Storage(path)


def test_an_empty_file_is_an_empty_store(tmp_path):
    """SQLite treats a zero-byte file as a new database; so does the store."""
    path = tmp_path / "captures.db"
    path.touch()
    assert Storage(path).count() == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_a_new_store_is_owner_only_whatever_the_umask(tmp_path):
    """The file will hold credentials in clear text. Its mode must come from the
    code, not from the environment: with the loosest possible umask it is still
    0600, and that is set at creation -- there is no window with a wider mode."""
    old = os.umask(0)
    try:
        path = tmp_path / "captures.db"
        Storage(path)
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_an_existing_store_keeps_the_mode_it_has(tmp_path):
    """Opening is not a chmod: a file the user has deliberately made readable by
    a group stays that way. The guarantee is for files this code creates."""
    path = tmp_path / "captures.db"
    Storage(path)
    os.chmod(path, 0o640)
    Storage(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
