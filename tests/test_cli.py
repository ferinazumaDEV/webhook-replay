from __future__ import annotations

import json

from webhook_replay import _color
from webhook_replay.cli import main, parse_header, parse_since
from webhook_replay.storage import Storage

_color.set_enabled(False)


def _seed(db_path) -> Storage:
    store = Storage(db_path)
    store.add(
        method="POST", path="/stripe", headers=[("Content-Type", "application/json")],
        body=b'{"amount": 100}', remote_addr="127.0.0.1",
    )
    store.add(
        method="GET", path="/health", headers=[], body=b"", remote_addr="127.0.0.1",
    )
    return store


def test_parse_since_relative():
    assert parse_since("10m") is not None
    assert parse_since(None) is None
    # ISO passthrough
    assert parse_since("2025-01-01T00:00:00+00:00") == "2025-01-01T00:00:00+00:00"


def test_parse_header():
    assert parse_header("X-Test: value") == ("X-Test", "value")


def test_cli_list_json(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    rc = main(["--no-color", "--db", str(db), "list", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 2
    assert {p["path"] for p in payload} == {"/stripe", "/health"}


def test_cli_list_filter(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    rc = main(["--db", str(db), "list", "--method", "POST", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["path"] == "/stripe"


def test_cli_show(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    rc = main(["--db", str(db), "show", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "/stripe" in out
    assert "amount" in out  # JSON body pretty-printed


def test_cli_show_missing_returns_1(tmp_path):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    assert main(["--db", str(db), "show", "999"]) == 1


def test_cli_curl(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    rc = main(["--db", str(db), "curl", "1", "--base", "http://localhost:7000"])
    assert rc == 0
    out = capsys.readouterr().out
    # shlex.quote leaves a metachar-free URL unquoted; the command is still valid.
    assert "curl -X POST http://localhost:7000/stripe" in out
    assert "--data-binary '{\"amount\": 100}'" in out


def test_cli_clear_with_yes(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    store = _seed(db)
    rc = main(["--db", str(db), "clear", "--yes"])
    assert rc == 0
    assert store.count() == 0


def test_cli_replay_end_to_end(tmp_path, capsys, receiver):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    rc = main(["--no-color", "--db", str(db), "replay", "1", "--to", receiver.base_url])
    assert rc == 0
    assert len(receiver.requests) == 1
    assert receiver.requests[0]["path"] == "/stripe"
    assert "200" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# output redaction
# --------------------------------------------------------------------------- #
import pytest  # noqa: E402

from webhook_replay.cli import parse_size  # noqa: E402

_SECRETS = ("EXAMPLE-NOT-A-REAL-TOKEN", "v1=abc123", "session=deadbeef")


def _seed_secrets(db_path) -> Storage:
    store = Storage(db_path)
    store.add(
        method="POST",
        path="/stripe",
        headers=[
            ("Content-Type", "application/json"),
            ("Authorization", "Bearer EXAMPLE-NOT-A-REAL-TOKEN"),
            ("Stripe-Signature", "t=1699,v1=abc123"),
            ("Cookie", "session=deadbeef"),
        ],
        body=b'{"amount": 100}',
        remote_addr="127.0.0.1",
    )
    return store


def test_cli_show_masks_secrets_by_default(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed_secrets(db)
    assert main(["--no-color", "--db", str(db), "show", "1"]) == 0
    out = capsys.readouterr().out
    for secret in _SECRETS:
        assert secret not in out
    assert "Authorization: Bearer <redacted>" in out
    assert "Stripe-Signature: <redacted>" in out
    assert "Content-Type: application/json" in out  # untouched
    assert "--show-secrets" in out  # the hint tells you how to opt out


def test_cli_show_with_show_secrets(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed_secrets(db)
    assert main(["--no-color", "--db", str(db), "show", "1", "--show-secrets"]) == 0
    out = capsys.readouterr().out
    for secret in _SECRETS:
        assert secret in out
    assert "<redacted>" not in out


def test_cli_list_json_masks_secrets_by_default(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed_secrets(db)
    assert main(["--no-color", "--db", str(db), "list", "--json"]) == 0
    captured = capsys.readouterr()
    for secret in _SECRETS:
        assert secret not in captured.out
    # stdout stays parseable JSON; the hint goes to stderr.
    payload = json.loads(captured.out)
    assert payload[0]["redacted"] is True
    assert ["Authorization", "Bearer <redacted>"] in payload[0]["headers"]
    assert "--show-secrets" in captured.err


def test_cli_list_json_with_show_secrets(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed_secrets(db)
    assert main(["--no-color", "--db", str(db), "list", "--json", "--show-secrets"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["redacted"] is False
    assert ["Authorization", "Bearer EXAMPLE-NOT-A-REAL-TOKEN"] in payload[0]["headers"]


def test_cli_curl_masks_secrets_by_default(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed_secrets(db)
    assert main(["--no-color", "--db", str(db), "curl", "1"]) == 0
    captured = capsys.readouterr()
    for secret in _SECRETS:
        assert secret not in captured.out
    assert "-H 'Authorization: Bearer <redacted>'" in captured.out
    assert "--show-secrets" in captured.err


def test_cli_curl_with_show_secrets(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed_secrets(db)
    assert main(["--no-color", "--db", str(db), "curl", "1", "--show-secrets"]) == 0
    out = capsys.readouterr().out
    assert "-H 'Authorization: Bearer EXAMPLE-NOT-A-REAL-TOKEN'" in out


def test_cli_no_hint_when_nothing_is_sensitive(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed(db)  # only Content-Type
    assert main(["--no-color", "--db", str(db), "curl", "1"]) == 0
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- #
# size parsing and prune
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1024", 1024),
        ("1KiB", 1024),
        ("1kb", 1000),
        ("1MiB", 1024 * 1024),
        ("2 MB", 2_000_000),
        ("0", 0),
    ],
)
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_parse_size_rejects_garbage():
    with pytest.raises(Exception):
        parse_size("lots")


def test_cli_prune_older_than(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    store = Storage(db)
    common = dict(
        method="POST", headers=[], body=b"{}", remote_addr="127.0.0.1",
    )
    store.add(path="/old", received_at="2020-01-01T00:00:00.000+00:00", **common)
    store.add(path="/new", received_at="2030-01-01T00:00:00.000+00:00", **common)

    assert main(["--no-color", "--db", str(db), "prune", "--older-than", "1d"]) == 0
    assert [r.path for r in store.list()] == ["/new"]


def test_cli_prune_keep(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    store = Storage(db)
    for i in range(5):
        store.add(
            method="POST", path=f"/h{i}", headers=[], body=b"{}", remote_addr="1.1.1.1"
        )
    assert main(["--no-color", "--db", str(db), "prune", "--keep", "2"]) == 0
    assert sorted(r.path for r in store.list()) == ["/h3", "/h4"]


def test_cli_prune_without_criteria_is_an_error(tmp_path):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    assert main(["--no-color", "--db", str(db), "prune"]) == 2
