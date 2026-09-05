from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import pytest

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
    # ISO input is normalised to UTC, millisecond precision.
    assert parse_since("2025-01-01T00:00:00+00:00") == "2025-01-01T00:00:00.000+00:00"


def test_parse_since_units_are_case_insensitive():
    lower = datetime.fromisoformat(parse_since("7d"))
    upper = datetime.fromisoformat(parse_since("7D"))
    assert abs((upper - lower).total_seconds()) < 5
    assert (datetime.now(timezone.utc) - upper).days == 7


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2025-01-01T00:00:00Z", "2025-01-01T00:00:00.000+00:00"),
        ("2025-01-01T00:00:00", "2025-01-01T00:00:00.000+00:00"),  # naive -> UTC
        ("2025-01-01T02:00:00+02:00", "2025-01-01T00:00:00.000+00:00"),
    ],
)
def test_parse_since_iso_is_normalised_to_utc(text, expected):
    assert parse_since(text) == expected


@pytest.mark.parametrize("text", ["yesterday", "garbage", "7 days", "7dd", "1w"])
def test_parse_since_rejects_garbage(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_since(text)


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


def test_cli_list_since_garbage_is_a_usage_error(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    with pytest.raises(SystemExit) as excinfo:
        main(["--no-color", "--db", str(db), "list", "--since", "garbage"])
    assert excinfo.value.code == 2
    assert "--since" in capsys.readouterr().err


def test_cli_prune_older_than_garbage_deletes_nothing(tmp_path, capsys):
    # Timestamps are compared as strings in SQLite, so an unparsed value used
    # to sort before every real timestamp and wipe the store with exit 0.
    db = tmp_path / "db.sqlite3"
    store = _seed(db)
    with pytest.raises(SystemExit) as excinfo:
        main(["--no-color", "--db", str(db), "prune", "--older-than", "yesterday"])
    assert excinfo.value.code == 2
    assert store.count() == 2


def test_cli_prune_older_than_is_case_insensitive(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    store = _seed(db)  # both received just now
    assert main(["--no-color", "--db", str(db), "prune", "--older-than", "7D"]) == 0
    assert store.count() == 2
    assert "pruned 0 request(s); 2 left." in capsys.readouterr().out


def test_cli_prune_keep_zero_is_a_usage_error(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    store = _seed(db)
    with pytest.raises(SystemExit) as excinfo:
        main(["--no-color", "--db", str(db), "prune", "--keep", "0"])
    assert excinfo.value.code == 2
    assert "use `clear`" in capsys.readouterr().err
    assert store.count() == 2


# --------------------------------------------------------------------------- #
# replay argument validation
# --------------------------------------------------------------------------- #
def test_cli_replay_ids_and_last_are_mutually_exclusive(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    with pytest.raises(SystemExit) as excinfo:
        main(["--no-color", "--db", str(db), "replay", "1", "--last", "1", "--to", "http://127.0.0.1:1"])
    assert excinfo.value.code == 2
    assert "not both" in capsys.readouterr().err


def test_cli_replay_without_targets_is_a_usage_error(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    with pytest.raises(SystemExit) as excinfo:
        main(["--no-color", "--db", str(db), "replay", "--to", "http://127.0.0.1:1"])
    assert excinfo.value.code == 2
    assert "--last" in capsys.readouterr().err


def test_cli_replay_times_zero_is_a_usage_error(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    with pytest.raises(SystemExit) as excinfo:
        main(["--no-color", "--db", str(db), "replay", "1", "--times", "0", "--to", "http://127.0.0.1:1"])
    assert excinfo.value.code == 2
    assert "--times" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# the rest of the documented command surface
# --------------------------------------------------------------------------- #
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
from pathlib import Path  # noqa: E402

from webhook_replay import __version__  # noqa: E402
from webhook_replay import cli  # noqa: E402


def test_cli_diff(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    store = Storage(db)
    common = dict(method="POST", path="/hook", headers=[], remote_addr="127.0.0.1")
    store.add(body=b'{"amount": 100, "currency": "usd"}', **common)
    store.add(body=b'{"currency": "eur", "amount": 100}', **common)
    assert main(["--no-color", "--db", str(db), "diff", "1", "2"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("-") and "usd" in line for line in lines)
    assert any(line.startswith("+") and "eur" in line for line in lines)
    # Same value, different key order: canonicalised away.
    assert not any("amount" in line and line[:1] in "+-" for line in lines)


def test_cli_diff_missing_id_returns_1(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    assert main(["--no-color", "--db", str(db), "diff", "1", "999"]) == 1
    assert "999" in capsys.readouterr().err


def test_cli_show_raw_writes_only_the_body(tmp_path, capsysbinary):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    assert main(["--no-color", "--db", str(db), "show", "1", "--raw"]) == 0
    assert capsysbinary.readouterr().out == b'{"amount": 100}'


def test_cli_replay_last_times_header_and_show_response(tmp_path, capsys, receiver):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    rc = main([
        "--no-color", "--db", str(db), "replay",
        "--last", "2", "--times", "2", "--header", "X-Debug: 1", "--show-response",
        "--to", receiver.base_url,
    ])
    assert rc == 0
    assert len(receiver.requests) == 4
    assert all(r["headers"].get("X-Debug") == "1" for r in receiver.requests)
    assert {r["path"] for r in receiver.requests} == {"/stripe", "/health"}
    out = capsys.readouterr().out
    assert "(x1)" in out and "(x2)" in out
    assert '{"ok": true}' in out  # --show-response prints the receiver's body


def test_cli_replay_method_override(tmp_path, capsys, receiver):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    assert main(["--no-color", "--db", str(db), "replay", "2", "--method", "POST", "--to", receiver.base_url]) == 0
    assert receiver.requests[0]["method"] == "POST"
    assert receiver.requests[0]["path"] == "/health"


def test_cli_replay_unknown_id_is_skipped_and_nothing_left_is_1(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    assert main(["--no-color", "--db", str(db), "replay", "999", "--to", "http://127.0.0.1:1"]) == 1
    err = capsys.readouterr().err
    assert "skipping unknown id 999" in err
    assert "nothing to replay" in err


def test_cli_list_since_filters_by_age(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    store = _seed(db)  # two captures received just now
    store.add(
        method="POST", path="/ancient", headers=[], body=b"{}", remote_addr="127.0.0.1",
        received_at="2020-01-01T00:00:00.000+00:00",
    )
    assert main(["--no-color", "--db", str(db), "list", "--since", "1h", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {p["path"] for p in payload} == {"/stripe", "/health"}


def test_cli_list_human_output(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    _seed(db)
    assert main(["--no-color", "--db", str(db), "list"]) == 0
    out = capsys.readouterr().out
    assert "#1" in out and "/stripe" in out and "2 request(s)." in out


def test_cli_clear_prompt_declined_keeps_everything(tmp_path, capsys, monkeypatch):
    db = tmp_path / "db.sqlite3"
    store = _seed(db)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert main(["--no-color", "--db", str(db), "clear"]) == 0
    assert store.count() == 2
    assert "aborted." in capsys.readouterr().out


def test_cli_clear_prompt_accepted(tmp_path, capsys, monkeypatch):
    db = tmp_path / "db.sqlite3"
    store = _seed(db)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert main(["--no-color", "--db", str(db), "clear"]) == 0
    assert store.count() == 0


def test_cli_version_in_process():
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_python_dash_m_reports_the_version():
    result = subprocess.run(
        [sys.executable, "-m", "webhook_replay", "--version"],
        capture_output=True, text=True, timeout=30,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"webhook-replay {__version__}"


def test_cli_serve_wires_limits_and_captures(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    seen: dict = {}
    real_create_server = cli.create_server

    def spy(store, **kwargs):
        server = real_create_server(store, **kwargs)
        seen["kwargs"] = kwargs
        seen["server"] = server
        return server

    monkeypatch.setattr(cli, "create_server", spy)
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(main([
            "--no-color", "--db", str(db), "serve",
            "--port", "0", "--max-body", "512KB", "--max-captures", "5",
            "--read-timeout", "0", "--status", "202", "--response", "queued",
        ])),
        daemon=True,
    )
    thread.start()
    deadline = time.perf_counter() + 5
    while "server" not in seen and time.perf_counter() < deadline:
        time.sleep(0.02)
    assert "server" in seen, "serve never built its server"
    try:
        host, port = seen["server"].server_address
        req = urllib.request.Request(
            f"http://{host}:{port}/hook", data=b'{"n": 1}', method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 202
            assert resp.read() == b"queued"
    finally:
        seen["server"].shutdown()
        thread.join(timeout=5)

    assert result == [0]
    kwargs = seen["kwargs"]
    assert kwargs["max_body_bytes"] == 512_000  # parse_size("512KB")
    assert kwargs["max_captures"] == 5
    assert kwargs["read_timeout"] == 0.0
    assert kwargs["response_status"] == 202
    assert kwargs["response_body"] == b"queued"
    assert kwargs["host"] == "127.0.0.1"
    assert Storage(db).get(1).body == b'{"n": 1}'
