"""Shared pytest fixtures."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from webhook_replay.storage import Storage


@pytest.fixture()
def store(tmp_path):
    return Storage(tmp_path / "captures.db")


class _RecordingReceiver:
    """A stand-in for the developer's local app, recording what it receives."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self, status: int = 200, body: bytes = b'{"ok": true}') -> None:
        received = self.requests

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(length) if length else b""
                received.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": dict(self.headers.items()),
                        "body": data,
                    }
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return

        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            setattr(Handler, f"do_{method}", Handler._handle)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


@pytest.fixture()
def receiver():
    rec = _RecordingReceiver()
    rec.start()
    try:
        yield rec
    finally:
        rec.stop()
