"""Re-send a captured request to a target URL."""
from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .storage import Header, WebhookRecord

# Headers that must not be replayed verbatim: they describe the *original*
# hop and are recomputed by urllib for the new request.
_STRIP_HEADERS = {"host", "content-length", "connection", "accept-encoding"}


@dataclass
class ReplayResult:
    """Outcome of replaying one request."""

    url: str
    method: str
    status: int
    reason: str
    body: bytes
    elapsed_ms: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 400

    def body_text(self, limit: int | None = None) -> str:
        try:
            text = self.body.decode("utf-8")
        except UnicodeDecodeError:
            return f"<{len(self.body)} bytes of binary data>"
        if limit is not None and len(text) > limit:
            return text[:limit] + f"... (+{len(text) - limit} more chars)"
        return text


def build_target_url(base_url: str, path: str) -> str:
    """Join a base URL with the original request path (which may hold a query)."""
    return base_url.rstrip("/") + path


def _replay_headers(
    record: WebhookRecord, extra: list[Header] | None
) -> list[Header]:
    headers = [
        (key, value)
        for key, value in record.headers
        if key.lower() not in _STRIP_HEADERS
    ]
    if extra:
        headers.extend(extra)
    return headers


def replay(
    record: WebhookRecord,
    base_url: str,
    *,
    timeout: float = 10.0,
    method: str | None = None,
    extra_headers: list[Header] | None = None,
) -> ReplayResult:
    """Replay ``record`` against ``base_url`` and capture the response."""
    url = build_target_url(base_url, record.path)
    verb = (method or record.method).upper()
    data = record.body if record.body else None

    request = urllib.request.Request(url=url, data=data, method=verb)
    for key, value in _replay_headers(record, extra_headers):
        request.add_header(key, value)

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            elapsed = (time.perf_counter() - started) * 1000
            return ReplayResult(
                url=url,
                method=verb,
                status=response.status,
                reason=response.reason or "",
                body=body,
                elapsed_ms=elapsed,
            )
    except urllib.error.HTTPError as exc:
        # A non-2xx response is a real, useful result — not a failure.
        body = exc.read()
        elapsed = (time.perf_counter() - started) * 1000
        return ReplayResult(
            url=url,
            method=verb,
            status=exc.code,
            reason=exc.reason or "",
            body=body,
            elapsed_ms=elapsed,
        )
    except (OSError, http.client.HTTPException) as exc:
        # Connection refused (URLError, an OSError), a target that accepts and
        # then stalls (TimeoutError) or closes without answering
        # (RemoteDisconnected / ConnectionResetError) are all reported, not
        # raised, so a `--times N` run keeps going.
        elapsed = (time.perf_counter() - started) * 1000
        return ReplayResult(
            url=url,
            method=verb,
            status=0,
            reason="",
            body=b"",
            elapsed_ms=elapsed,
            error=str(getattr(exc, "reason", exc)),
        )
