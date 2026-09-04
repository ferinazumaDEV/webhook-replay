"""The local capture server.

A tiny :mod:`http.server` handler that accepts *any* method on *any* path,
stores the request via :class:`~webhook_replay.storage.Storage`, and replies
with a configurable canned response so the sender is satisfied.

Captures are stored verbatim — headers and body exactly as they arrived, so a
replay is byte-for-byte faithful. Three bounds keep that from turning into an
unbounded local sink: a maximum body size (answered with ``413``), a maximum
number of retained captures (oldest evicted first), and a socket timeout so a
slow or stalled sender cannot pin a handler thread forever.
"""
from __future__ import annotations

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

        def _capture(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._reject(400, "malformed Content-Length")
                return
            if length < 0:
                self._reject(400, "malformed Content-Length")
                return

            if max_body_bytes and length > max_body_bytes:
                # Refuse before reading: the point of the cap is not to buffer
                # an oversized body in the first place.
                self._reject(
                    413, f"body of {length} bytes exceeds the {max_body_bytes} byte limit"
                )
                return

            try:
                body = self._read_body(length) if length > 0 else b""
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
) -> ThreadingHTTPServer:
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
    return ThreadingHTTPServer((host, port), handler)
