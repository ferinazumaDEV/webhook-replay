"""The local capture server.

A tiny :mod:`http.server` handler that accepts *any* method on *any* path,
stores the request via :class:`~webhook_replay.storage.Storage`, and replies
with a configurable canned response so the sender is satisfied.

Captures are stored verbatim — headers and body exactly as they arrived, so a
replay is byte-for-byte faithful. The body is read from ``Content-Length`` or
decoded from a ``Transfer-Encoding: chunked`` stream; a request carrying both
headers, or a malformed length, is refused with ``400``. Three bounds keep the
store from turning into an unbounded local sink: a maximum body size (answered
with ``413``, for chunked bodies as soon as the declared chunks pass the cap),
a maximum number of retained captures (oldest evicted first), and a socket
timeout so a slow or stalled sender cannot pin a handler thread forever. Every
response closes the connection, so one connection serves exactly one request.
"""
from __future__ import annotations

import re
import socket
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import _color
from .storage import Storage

# Methods we register a handler for.
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

#: Largest request body accepted, in bytes. Bigger ones get a ``413`` and are
#: not stored. Webhook payloads are kilobytes; a megabyte is already generous.
DEFAULT_MAX_BODY_BYTES = 1024 * 1024

#: How many captures to retain. Once the store is full, each new capture evicts
#: the oldest one. ``0`` disables the cap.
DEFAULT_MAX_CAPTURES = 1000

#: Socket timeout, in seconds, for reading a request. ``0`` disables it.
DEFAULT_READ_TIMEOUT = 30.0

# How much body to pull per read() call while honouring the size cap.
_READ_CHUNK = 64 * 1024

# Longest chunk-size / trailer line accepted in a chunked body (http.client
# uses the same bound), and how many trailer lines before we give up.
_MAX_LINE = 65536
_MAX_TRAILERS = 100

# RFC 9110 s8.6: Content-Length is 1*DIGIT -- no sign, no underscore, ASCII only.
_DIGITS_RE = re.compile(r"[0-9]+")
_HEX_RE = re.compile(rb"[0-9a-fA-F]+")


class _BadRequest(Exception):
    """Malformed request framing; answered with ``400``."""


class _TooLarge(Exception):
    """Body over ``max_body_bytes``; answered with ``413``."""

OnCapture = Callable[[int, str, str, int], None]
OnReject = Callable[[str, str, int, str], None]


def _default_logger(request_id: int, method: str, path: str, size: int) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(
        f"{_color.paint(stamp, 'gray')} "
        f"{_color.paint('#' + str(request_id), 'bold')} "
        f"{_color.method(method)} {path} "
        f"{_color.paint(f'({size} bytes)', 'dim')}",
        flush=True,
    )


def _default_reject_logger(method: str, path: str, status: int, reason: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(
        f"{_color.paint(stamp, 'gray')} "
        f"{_color.paint('--', 'bold')} "
        f"{_color.method(method)} {path} "
        f"{_color.paint(f'{status} {reason}', 'red')}",
        flush=True,
    )


def make_handler(
    storage: Storage,
    *,
    response_status: int = 200,
    response_body: bytes = b'{"received": true}',
    response_content_type: str = "application/json",
    on_capture: OnCapture | None = _default_logger,
    on_reject: OnReject | None = _default_reject_logger,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_captures: int = DEFAULT_MAX_CAPTURES,
    read_timeout: float | None = DEFAULT_READ_TIMEOUT,
) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to ``storage``.

    ``max_body_bytes`` caps the accepted body (``0`` to disable),
    ``max_captures`` caps how many captures are retained (``0`` to disable), and
    ``read_timeout`` is the socket timeout in seconds (``0``/``None`` to
    disable).
    """
    timeout = read_timeout if read_timeout else None

    class WebhookCaptureHandler(BaseHTTPRequestHandler):
        # A friendlier Server: header than the default BaseHTTP/x.y string.
        server_version = "webhook-replay/0"
        sys_version = ""
        # HTTP/1.1 so a sender's `Expect: 100-continue` is answered at once
        # instead of after its expect-timeout (about a second with curl).
        # Every response carries `Connection: close`, so a connection still
        # serves exactly one request and no handler thread is left waiting
        # for a second one that never comes.
        protocol_version = "HTTP/1.1"
        # socketserver applies this to the connection in setup(), so a sender
        # that opens a socket and then stalls cannot hold the thread forever.
        timeout = None

        def _reject(self, status: int, reason: str) -> None:
            """Answer with an error status and close the connection."""
            self.close_connection = True
            payload = f"{reason}\n".encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
            except OSError:
                # The sender may already be gone; nothing useful left to do.
                pass
            if on_reject is not None:
                on_reject(self.command, self.path, status, reason)

        def _read_body(self, length: int) -> bytes:
            """Read exactly ``length`` bytes, in chunks."""
            chunks: list[bytes] = []
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, _READ_CHUNK))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        def _read_chunked_body(self) -> bytes:
            """Decode a ``Transfer-Encoding: chunked`` body, honouring the cap.

            Raises :class:`_BadRequest` on malformed framing and
            :class:`_TooLarge` as soon as the declared chunks exceed
            ``max_body_bytes``, before that data is buffered.
            """
            chunks: list[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline(_MAX_LINE + 1)
                if len(line) > _MAX_LINE:
                    raise _BadRequest("chunk-size line too long")
                size_text = line.split(b";", 1)[0].strip()
                if not _HEX_RE.fullmatch(size_text):
                    raise _BadRequest("malformed chunk size")
                size = int(size_text, 16)
                if size == 0:
                    break
                total += size
                if max_body_bytes and total > max_body_bytes:
                    raise _TooLarge()
                data = self._read_body(size)
                if len(data) != size or self.rfile.read(2) != b"\r\n":
                    raise _BadRequest("truncated chunk")
                chunks.append(data)
            # Trailer section: optional header lines, then the blank line
            # that ends the message.
            for _ in range(_MAX_TRAILERS):
                line = self.rfile.readline(_MAX_LINE + 1)
                if not line or len(line) > _MAX_LINE:
                    raise _BadRequest("malformed chunked trailer")
                if line in (b"\r\n", b"\n"):
                    return b"".join(chunks)
            raise _BadRequest("too many chunked trailers")

        def handle_expect_100(self) -> bool:
            # Refuse an oversized announcement before the sender ships the
            # body -- that is what `Expect: 100-continue` is for. Anything
            # else gets the standard `100 Continue`; the full validation of
            # the headers happens in _capture.
            values = self.headers.get_all("Content-Length") or []
            if (
                max_body_bytes
                and len(values) == 1
                and _DIGITS_RE.fullmatch(values[0].strip())
                and int(values[0].strip()) > max_body_bytes
            ):
                self._reject(
                    413,
                    f"body of {values[0].strip()} bytes exceeds the "
                    f"{max_body_bytes} byte limit",
                )
                return False
            return super().handle_expect_100()

        def _capture(self) -> None:
            lengths = self.headers.get_all("Content-Length") or []
            codings = self.headers.get_all("Transfer-Encoding") or []
            if lengths and codings:
                # RFC 9112 s6.3: the pair is ambiguous framing and the classic
                # request-smuggling ingredient; refuse rather than guess.
                self._reject(400, "Content-Length and Transfer-Encoding both present")
                return
            if len(lengths) > 1 or (
                lengths and not _DIGITS_RE.fullmatch(lengths[0].strip())
            ):
                # Duplicate, signed, underscored or non-ASCII lengths all
                # capture the wrong bytes; RFC 9110 s8.6 says reject.
                self._reject(400, "malformed Content-Length")
                return
            chunked = False
            if codings:
                names = [c.strip().lower() for value in codings for c in value.split(",")]
                if names != ["chunked"]:
                    self._reject(501, "unsupported Transfer-Encoding")
                    return
                chunked = True
            length = int(lengths[0].strip()) if lengths else 0

            if max_body_bytes and length > max_body_bytes:
                # Refuse before reading: the point of the cap is not to buffer
                # an oversized body in the first place.
                self._reject(
                    413, f"body of {length} bytes exceeds the {max_body_bytes} byte limit"
                )
                return

            try:
                if chunked:
                    body = self._read_chunked_body()
                else:
                    body = self._read_body(length) if length > 0 else b""
            except _TooLarge:
                self._reject(
                    413, f"chunked body exceeds the {max_body_bytes} byte limit"
                )
                return
            except _BadRequest as exc:
                self._reject(400, str(exc))
                return
            except socket.timeout:
                # socket.timeout is TimeoutError on 3.10+; on 3.9 it is an OSError.
                self._reject(408, "timed out reading the request body")
                return
            except OSError:
                self.close_connection = True
                return

            headers = [(key, value) for key, value in self.headers.items()]
            request_id = storage.add(
                method=self.command,
                path=self.path,
                headers=headers,
                body=body,
                remote_addr=self.client_address[0],
            )
            if max_captures:
                storage.prune_to(max_captures)
            if on_capture is not None:
                on_capture(request_id, self.command, self.path, len(body))

            payload = b"" if self.command == "HEAD" else response_body
            self.send_response(response_status)
            self.send_header("Content-Type", response_content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Webhook-Replay-Id", str(request_id))
            # One request per connection, whatever the sender asked for.
            self.send_header("Connection", "close")
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        # Silence the default stderr access log; we log our own line.
        def log_message(self, *args: object) -> None:  # noqa: D401
            return

    WebhookCaptureHandler.timeout = timeout

    for method in _METHODS:
        setattr(WebhookCaptureHandler, f"do_{method}", WebhookCaptureHandler._capture)

    return WebhookCaptureHandler


class CaptureServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that can actually absorb a burst.

    socketserver's default ``request_queue_size`` is **5**: the kernel accepts
    five pending connections and refuses or resets the rest before any handler
    thread sees them. That is the wrong default for this program. A webhook
    sender does not arrive politely one at a time — a fan-out, or a provider
    working through a retry backlog, opens many connections at once, and every
    one past the fifth is a capture silently lost. Losing captures is the one
    thing this tool exists not to do.

    Measured on a 2-core machine, six rounds each, counting failed connections:

        burst  backlog=5   backlog=SOMAXCONN
           20          6                   0
           60        173                   0
          150        649                   0
          300       1448                   0

    Twenty simultaneous senders is enough to lose data at the default, which is
    also why ``test_concurrent_captures_are_all_persisted`` was intermittently
    red in CI: the test was right and the server was wrong.

    ``socket.SOMAXCONN`` asks for as deep a queue as the OS allows rather than
    guessing a number; the kernel silently caps it to its own maximum, so this
    is safe on platforms where that maximum is small.
    """

    request_queue_size = socket.SOMAXCONN


def create_server(
    storage: Storage,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    response_status: int = 200,
    response_body: bytes = b'{"received": true}',
    on_capture: OnCapture | None = _default_logger,
    on_reject: OnReject | None = _default_reject_logger,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_captures: int = DEFAULT_MAX_CAPTURES,
    read_timeout: float | None = DEFAULT_READ_TIMEOUT,
) -> CaptureServer:
    """Create (but do not start) a capture server.

    Pass ``port=0`` to bind an OS-assigned free port; read the real port from
    ``server.server_address[1]`` afterwards.
    """
    handler = make_handler(
        storage,
        response_status=response_status,
        response_body=response_body,
        on_capture=on_capture,
        on_reject=on_reject,
        max_body_bytes=max_body_bytes,
        max_captures=max_captures,
        read_timeout=read_timeout,
    )
    return CaptureServer((host, port), handler)
