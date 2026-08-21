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
