"""The local capture server.

A tiny :mod:`http.server` handler that accepts *any* method on *any* path,
stores the request via :class:`~webhook_replay.storage.Storage`, and replies
with a configurable canned response so the sender is satisfied.
"""
from __future__ import annotations

from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import _color
from .storage import Storage

# Methods we register a handler for.
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

OnCapture = Callable[[int, str, str, int], None]


def _default_logger(request_id: int, method: str, path: str, size: int) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(
        f"{_color.paint(stamp, 'gray')} "
        f"{_color.paint('#' + str(request_id), 'bold')} "
        f"{_color.method(method)} {path} "
        f"{_color.paint(f'({size} bytes)', 'dim')}",
        flush=True,
    )


def make_handler(
    storage: Storage,
    *,
    response_status: int = 200,
    response_body: bytes = b'{"received": true}',
    response_content_type: str = "application/json",
    on_capture: OnCapture | None = _default_logger,
) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to ``storage``."""

    class WebhookCaptureHandler(BaseHTTPRequestHandler):
        # A friendlier Server: header than the default BaseHTTP/x.y string.
        server_version = "webhook-replay/0"
        sys_version = ""

        def _capture(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            headers = [(key, value) for key, value in self.headers.items()]
            request_id = storage.add(
                method=self.command,
                path=self.path,
                headers=headers,
                body=body,
                remote_addr=self.client_address[0],
            )
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
    )
    return ThreadingHTTPServer((host, port), handler)
