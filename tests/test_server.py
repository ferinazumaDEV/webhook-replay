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


# --------------------------------------------------------------------------- #
# limits
# --------------------------------------------------------------------------- #
import contextlib  # noqa: E402
import socket  # noqa: E402
import urllib.error  # noqa: E402

from webhook_replay.server import (  # noqa: E402
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_CAPTURES,
)


@contextlib.contextmanager
def _serving(store, **kwargs):
    """Run a capture server with custom limits on an OS-assigned port."""
    kwargs.setdefault("on_capture", None)
    kwargs.setdefault("on_reject", None)
    server = create_server(store, port=0, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_defaults_are_bounded():
    # A capture server with no arguments is not an unbounded sink.
    assert DEFAULT_MAX_BODY_BYTES == 1024 * 1024
    assert DEFAULT_MAX_CAPTURES == 1000


def test_oversized_body_is_rejected_with_413(store):
    with _serving(store, max_body_bytes=1024) as base:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(base + "/big", b"x" * 2048)
        assert excinfo.value.code == 413
    # Rejected requests are not persisted.
    assert store.count() == 0


def test_body_at_the_limit_is_still_accepted(store):
    with _serving(store, max_body_bytes=1024) as base:
        status, _, _ = _post(base + "/ok", b"x" * 1024)
        assert status == 200
    assert store.count() == 1
    assert len(store.get(1).body) == 1024


def test_body_limit_can_be_disabled(store):
    with _serving(store, max_body_bytes=0) as base:
        status, _, _ = _post(base + "/ok", b"x" * 5000)
        assert status == 200
    assert store.count() == 1


def test_retention_limit_discards_the_oldest(store):
    with _serving(store, max_captures=3) as base:
        for i in range(6):
            _post(base + f"/hook/{i}", b"{}")

    assert store.count() == 3
    # The three most recent survive; the first three were evicted.
    assert sorted(r.path for r in store.list()) == ["/hook/3", "/hook/4", "/hook/5"]


def test_retention_limit_can_be_disabled(store):
    with _serving(store, max_captures=0) as base:
        for i in range(5):
            _post(base + f"/hook/{i}", b"{}")
    assert store.count() == 5


def test_read_timeout_drops_a_stalled_sender(store):
    # Announce a body, send nothing: the handler must give up, not hang.
    with _serving(store, read_timeout=0.5) as base:
        host, port = base.rsplit(":", 1)
        sock = socket.create_connection((host.rsplit("/", 1)[-1], int(port)), timeout=5)
        try:
            sock.sendall(
                b"POST /slow HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n"
            )
            sock.settimeout(5)
            # The server closes (or answers 408) well before our own timeout.
            reply = sock.recv(1024)
        finally:
            sock.close()
    assert reply == b"" or b"408" in reply
    assert store.count() == 0


def test_malformed_content_length_is_rejected(store):
    with _serving(store) as base:
        host, port = base.rsplit(":", 1)
        sock = socket.create_connection((host.rsplit("/", 1)[-1], int(port)), timeout=5)
        try:
            sock.sendall(
                b"POST /bad HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n"
            )
            sock.settimeout(5)
            reply = sock.recv(1024)
        finally:
            sock.close()
    assert b"400" in reply
    assert store.count() == 0


# --------------------------------------------------------------------------- #
# request framing: chunked bodies, Content-Length validation, one request per
# connection, Expect: 100-continue
# --------------------------------------------------------------------------- #
import time  # noqa: E402


def _connect(base: str, timeout: float = 5) -> socket.socket:
    host, port = base.rsplit(":", 1)
    return socket.create_connection((host.rsplit("/", 1)[-1], int(port)), timeout=timeout)


def _read_all(sock: socket.socket, timeout: float = 5) -> bytes:
    """Read until the server closes the connection (or ``timeout`` passes)."""
    sock.settimeout(timeout)
    chunks: list[bytes] = []
    while True:
        try:
            data = sock.recv(65536)
        except socket.timeout:
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def _raw(base: str, payload: bytes) -> bytes:
    """Send raw bytes to the capture server and return its whole reply."""
    sock = _connect(base)
    try:
        try:
            sock.sendall(payload)
        except OSError:
            # The server may already have answered and closed (e.g. 413).
            pass
        return _read_all(sock)
    finally:
        sock.close()


def _status(reply: bytes) -> int:
    return int(reply.split(b" ", 2)[1])


def _chunked(body: bytes, chunk: int = 4096) -> bytes:
    out = b""
    for i in range(0, len(body), chunk):
        piece = body[i : i + chunk]
        out += f"{len(piece):x}\r\n".encode() + piece + b"\r\n"
    return out + b"0\r\n\r\n"


def test_chunked_body_is_decoded_and_stored(store):
    with _serving(store) as base:
        reply = _raw(
            base,
            b"POST /c HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n",
        )
    assert _status(reply) == 200
    assert store.count() == 1
    record = store.get(1)
    assert record.body == b"hello world"
    # Headers are still stored as they arrived.
    assert record.header("Transfer-Encoding") == "chunked"


def test_chunked_body_with_trailers_is_accepted(store):
    with _serving(store) as base:
        reply = _raw(
            base,
            b"POST /t HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"3\r\nabc\r\n0\r\nX-Checksum: 1\r\n\r\n",
        )
    assert _status(reply) == 200
    assert store.get(1).body == b"abc"


def test_chunked_body_over_the_cap_is_rejected_with_413(store):
    with _serving(store, max_body_bytes=1024) as base:
        reply = _raw(
            base,
            b"POST /big HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            + _chunked(b"x" * 4096),
        )
    assert _status(reply) == 413
    assert store.count() == 0


def test_chunked_body_at_the_cap_is_accepted(store):
    with _serving(store, max_body_bytes=1024) as base:
        reply = _raw(
            base,
            b"POST /ok HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            + _chunked(b"x" * 1024, chunk=512),
        )
    assert _status(reply) == 200
    assert len(store.get(1).body) == 1024


@pytest.mark.parametrize(
    "framing",
    [
        b"zz\r\nhello\r\n0\r\n\r\n",  # non-hex chunk size
        b"5\r\nhelloXX0\r\n\r\n",  # missing CRLF after the chunk data
    ],
)
def test_malformed_chunked_body_is_rejected_with_400(store, framing):
    with _serving(store) as base:
        reply = _raw(
            base,
            b"POST /bad HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n" + framing,
        )
    assert _status(reply) == 400
    assert store.count() == 0


def test_truncated_chunked_body_is_rejected_with_400(store):
    # The sender announces five bytes, ships three and hangs up.
    with _serving(store) as base:
        sock = _connect(base)
        try:
            sock.sendall(
                b"POST /cut HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"5\r\nhel"
            )
            sock.shutdown(socket.SHUT_WR)
            reply = _read_all(sock)
        finally:
            sock.close()
    assert _status(reply) == 400
    assert store.count() == 0


def test_content_length_with_transfer_encoding_is_rejected_with_400(store):
    # RFC 9112 s6.3: ambiguous framing, the request-smuggling ingredient.
    with _serving(store) as base:
        reply = _raw(
            base,
            b"POST /both HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n",
        )
    assert _status(reply) == 400
    assert store.count() == 0


def test_unsupported_transfer_encoding_is_rejected_with_501(store):
    with _serving(store) as base:
        reply = _raw(
            base,
            b"POST /gz HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: gzip\r\n\r\n",
        )
    assert _status(reply) == 501
    assert store.count() == 0


def test_pipelined_second_request_is_not_processed(store):
    # Exactly one request per connection: the response closes it, so a
    # request smuggled behind the first one never reaches the store.
    with _serving(store) as base:
        reply = _raw(
            base,
            b"POST /e1 HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"2\r\nhi\r\n0\r\n\r\n"
            b"POST /e2 HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n",
        )
    assert reply.count(b"HTTP/1.1 ") == 1
    assert store.count() == 1
    assert store.get(1).path == "/e1"


@pytest.mark.parametrize(
    "header",
    [
        b"Content-Length: 1_000",
        b"Content-Length: +5",
        b"Content-Length: -0",
        b"Content-Length: 5\r\nContent-Length: 10",
        b"Content-Length: 0x5",
        b"Content-Length: ",
    ],
)
def test_non_canonical_content_length_is_rejected_with_400(store, header):
    # RFC 9110 s8.6: Content-Length is 1*DIGIT and duplicates must agree.
    # int() alone accepted every one of these, storing the wrong bytes.
    with _serving(store) as base:
        reply = _raw(
            base, b"POST /cl HTTP/1.1\r\nHost: x\r\n" + header + b"\r\n\r\n0123456789"
        )
    assert _status(reply) == 400
    assert store.count() == 0


def test_response_closes_the_connection_even_if_keep_alive_was_asked(store):
    with _serving(store) as base:
        sock = _connect(base)
        try:
            sock.sendall(
                b"POST /ka HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n"
                b"Content-Length: 2\r\n\r\n{}"
            )
            started = time.perf_counter()
            reply = _read_all(sock, timeout=3)
            elapsed = time.perf_counter() - started
        finally:
            sock.close()
    assert _status(reply) == 200
    assert b"connection: close" in reply.lower()
    # EOF arrived with the response, not after a keep-alive wait.
    assert elapsed < 1.0
    assert store.count() == 1


def test_expect_100_continue_is_answered_immediately(store):
    with _serving(store) as base:
        sock = _connect(base)
        try:
            sock.sendall(
                b"POST /x HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\n"
                b"Content-Length: 5\r\n\r\n"
            )
            sock.settimeout(1)
            started = time.perf_counter()
            interim = sock.recv(1024)  # would time out if no 100 were sent
            elapsed = time.perf_counter() - started
            sock.sendall(b"hello")
            reply = _read_all(sock)
        finally:
            sock.close()
    assert interim.startswith(b"HTTP/1.1 100")
    assert elapsed < 0.5
    assert _status(reply) == 200
    assert store.get(1).body == b"hello"


def test_expect_100_continue_with_oversized_length_is_refused_before_the_body(store):
    with _serving(store, max_body_bytes=1024) as base:
        reply = _raw(
            base,
            b"POST /x HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\n"
            b"Content-Length: 4096\r\n\r\n",
        )
    assert _status(reply) == 413
    assert b"100 Continue" not in reply
    assert store.count() == 0
