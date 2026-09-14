"""Adversarial inputs: what a hostile, or merely broken, sender and operator produce.

The 2026-09-12 external audit fuzzed typedout, scaffld and framesig and found seven bugs; it did
not look here. These tests state what a caller is entitled to rely on and feed the code the inputs
that stretch it: a ``Content-Length`` longer than ``int()`` converts, a body cut short by the sender,
a ``--max-body`` past float range, a ``--header`` with a line break in it, and the credential-bearing
header names the redaction rule did not know.

Written against 0.1.2 as it was: the first run of this file failed on every test that is not
marked as a control. Two of those failures were not assertion errors but a traceback printed from
the handler thread and a connection closed with no response at all.
"""
from __future__ import annotations

import argparse
import contextlib
import socket
import threading

import pytest

from webhook_replay.cli import build_parser, parse_header, parse_since, parse_size
from webhook_replay.redact import is_sensitive
from webhook_replay.server import create_server


# --- helpers ------------------------------------------------------------------------------------ #

@contextlib.contextmanager
def _serving(store, **kwargs):
    server = create_server(
        store, port=0, on_capture=None, on_reject=None, read_timeout=2, **kwargs
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield host, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _raw(host: str, port: int, payload: bytes, *, half_close: bool = False) -> bytes:
    """Send raw bytes and return the whole reply (empty if the server just closed)."""
    sock = socket.create_connection((host, port), timeout=5)
    try:
        sock.sendall(payload)
        if half_close:
            sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)
    finally:
        sock.close()


def _statuses(reply: bytes) -> list[int]:
    """Every status line in the reply, in order (``Expect`` replies carry two)."""
    return [int(line.split(b" ", 2)[1]) for line in reply.split(b"\r\n") if line.startswith(b"HTTP/")]


_REQUEST = b"POST /hook HTTP/1.1\r\nHost: x\r\n"


# --- serve: a Content-Length the interpreter refuses to convert ------------------------------- #

def test_content_length_longer_than_int_converts_is_handled_not_a_traceback(store, capfd):
    """5001 characters: ``int()`` refuses strings past 4300, and that used to escape the handler.

    The grammar (``1*DIGIT``) says ``000...01`` is one byte, so once leading zeros are dropped
    before conversion the request is simply a one-byte capture."""
    numeral = b"0" * 5000 + b"1"
    with _serving(store) as (host, port):
        reply = _raw(host, port, _REQUEST + b"Content-Length: " + numeral + b"\r\n\r\nA")
    assert _statuses(reply) == [200], reply[:80]
    assert store.count() == 1
    assert store.latest(1)[0].body == b"A"
    assert "Traceback" not in capfd.readouterr().err


def test_expect_100_with_an_unconvertible_content_length_is_handled_not_a_traceback(store, capfd):
    numeral = b"0" * 5000 + b"1"
    with _serving(store) as (host, port):
        reply = _raw(
            host, port, _REQUEST + b"Expect: 100-continue\r\nContent-Length: " + numeral + b"\r\n\r\nA"
        )
    assert _statuses(reply) == [100, 200], reply[:80]
    assert store.count() == 1
    assert "Traceback" not in capfd.readouterr().err


def test_content_length_of_twenty_one_significant_digits_is_malformed(store):
    with _serving(store) as (host, port):
        reply = _raw(host, port, _REQUEST + b"Content-Length: " + b"1" * 21 + b"\r\n\r\n")
    assert _statuses(reply) == [400]
    assert store.count() == 0


def test_content_length_of_twenty_digits_is_merely_too_large(store):
    """The bound is about conversion, not policy: a huge but convertible length still gets the 413."""
    with _serving(store) as (host, port):
        reply = _raw(host, port, _REQUEST + b"Content-Length: " + b"1" * 20 + b"\r\n\r\n")
    assert _statuses(reply) == [413]


def test_leading_zeros_do_not_count_towards_the_bound(store):
    """``0000...04`` is a valid 1*DIGIT numeral for four bytes (control: this passed before too)."""
    with _serving(store) as (host, port):
        reply = _raw(host, port, _REQUEST + b"Content-Length: " + b"0" * 40 + b"4\r\n\r\nabcd")
    assert _statuses(reply) == [200]
    assert store.count() == 1
    assert store.latest(1)[0].body == b"abcd"


# --- serve: a body the sender never finished ------------------------------------------------- #

def test_body_shorter_than_content_length_is_not_a_capture(store):
    """RFC 9112 s6.3: the message is incomplete. It used to be stored as ``abcd`` and answered 200."""
    with _serving(store) as (host, port):
        reply = _raw(host, port, _REQUEST + b"Content-Length: 10\r\n\r\nabcd", half_close=True)
    assert _statuses(reply) == [400], reply[:80]
    assert b"body truncated: 4 of 10 bytes received" in reply
    assert store.count() == 0


def test_complete_body_is_still_a_capture(store):
    """Control for the test above: same bytes, honest length."""
    with _serving(store) as (host, port):
        reply = _raw(host, port, _REQUEST + b"Content-Length: 4\r\n\r\nabcd", half_close=True)
    assert _statuses(reply) == [200]
    assert store.count() == 1


# --- CLI: values argparse must turn into usage errors, never tracebacks ----------------------- #

@pytest.mark.parametrize(
    "value",
    ["1" + "0" * 400, "1" + "0" * 5000, "9" * 20 + "GiB", "1e400"],
    ids=["1e400-as-digits", "5001-digits", "20-digits-GiB", "1e400-literal"],
)
def test_max_body_beyond_any_real_size_is_a_usage_error(value):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_size(value)


def test_sizes_are_parsed_exactly():
    """float("1e30") is 1000000000000000019884624838656; the operator wrote 1 followed by 30 zeros."""
    assert parse_size("1" + "0" * 18) == 10**18
    assert parse_size("1.5kb") == 1500
    assert parse_size("0") == 0
    assert parse_size("1 GiB") == 1024**3


@pytest.mark.parametrize("value", ["99999999999d", "1000000000d", "999999999d", "9999999999h"])
def test_since_beyond_timedelta_range_is_a_usage_error(value):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_since(value)


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
def test_since_timestamp_with_no_utc_form_is_a_usage_error(value):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_since(value)


def test_since_within_range_still_parses():
    assert parse_since("7d") is not None
    assert parse_since("2026-09-13T10:00:00Z") == "2026-09-13T10:00:00.000+00:00"


@pytest.mark.parametrize("raw", [":x", " : x", "Bad Name: x", "X-A: v\r\nInjected: y", "X-A: a\nb", "X-A: a\x00b"])
def test_header_the_target_would_refuse_is_a_usage_error(raw):
    """Empty names and line breaks used to surface as ValueError from http.client mid-replay."""
    with pytest.raises(argparse.ArgumentTypeError):
        parse_header(raw)


def test_header_well_formed_still_parses():
    assert parse_header("X-Debug: 1") == ("X-Debug", "1")
    assert parse_header("X-Debug:") == ("X-Debug", "")
    assert parse_header("x_custom.name: a: b") == ("x_custom.name", "a: b")


def test_bad_header_on_the_command_line_exits_2_not_with_a_traceback(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["replay", "1", "--to", "http://127.0.0.1:1", "--header", ":x"])
    assert exc.value.code == 2
    assert "invalid header name" in capsys.readouterr().err


# --- redaction: credential-bearing names the rule did not know -------------------------------- #

@pytest.mark.parametrize("name", [
    "X-Auth-Key",                  # Cloudflare global API key
    "Ocp-Apim-Subscription-Key",   # Azure API Management
    "X-Forwarded-Authorization",   # proxies that relay the original credential
    "X-Original-Authorization",
    "X-Password", "X-Passwd", "X-Credential", "X-Credentials",
    "X-Access-Key", "X-Private-Key", "X-Shared-Key", "X-Client-Key", "X-Session-Key",
    "Access-Key", "Auth",
])
def test_credential_bearing_names_are_masked(name):
    assert is_sensitive(name)


@pytest.mark.parametrize("name", [
    "Idempotency-Key",   # a request identifier, and useful in a bug report
    "X-Request-Id", "X-Session-Id", "X-Client-Id",
    "X-Author",          # contains "auth", not as a segment
    "X-Design-Id", "Content-Type", "Cache-Key",
])
def test_identifiers_stay_readable(name):
    assert not is_sensitive(name)
