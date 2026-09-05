"""SQLite-backed persistence for captured webhook requests.

Every method opens a short-lived connection so the store is safe to use from
the threaded capture server (one request handler thread per connection).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

Header = tuple[str, str]

DEFAULT_DB = Path.home() / ".webhook-replay" / "captures.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    headers     TEXT NOT NULL,
    body        BLOB NOT NULL,
    remote_addr TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_received_at ON requests(received_at);
"""


@dataclass(frozen=True)
class WebhookRecord:
    """A single captured HTTP request."""

    id: int
    method: str
    path: str
    headers: list[Header]
    body: bytes
    remote_addr: str
    received_at: str

    @property
    def content_type(self) -> str | None:
        for key, value in self.headers:
            if key.lower() == "content-type":
                return value
        return None

    def header(self, name: str) -> str | None:
        name = name.lower()
        for key, value in self.headers:
            if key.lower() == name:
                return value
        return None

    def body_text(self) -> str | None:
        """Decode the body as UTF-8, or ``None`` if it is binary."""
        try:
            return self.body.decode("utf-8")
        except UnicodeDecodeError:
            return None


def _utcnow_iso() -> str:
    # Millisecond precision keeps ordering stable and reads cleanly.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Storage:
    """A thin persistence layer over a SQLite file."""

    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def add(
        self,
        *,
        method: str,
        path: str,
        headers: list[Header],
        body: bytes,
        remote_addr: str,
        received_at: str | None = None,
    ) -> int:
        """Persist a captured request and return its new id."""
        received_at = received_at or _utcnow_iso()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO requests (method, path, headers, body, remote_addr, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    method,
                    path,
                    json.dumps(headers),
                    sqlite3.Binary(body),
                    remote_addr,
                    received_at,
                ),
            )
            return int(cur.lastrowid)

    def get(self, request_id: int) -> WebhookRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def list(
        self,
        *,
        method: str | None = None,
        path_contains: str | None = None,
        since: str | None = None,
        limit: int | None = None,
        newest_first: bool = True,
    ) -> list[WebhookRecord]:
        """Return captured requests, optionally filtered."""
        clauses: list[str] = []
        params: list[object] = []
        if method:
            clauses.append("method = ?")
            params.append(method.upper())
        if path_contains:
            # Escape LIKE's own wildcards so `_` and `%` in the filter match
            # themselves: the CLI documents --path as a plain substring.
            escaped = (
                path_contains.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            clauses.append("path LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        if since:
            clauses.append("received_at >= ?")
            params.append(since)

        sql = "SELECT * FROM requests"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id " + ("DESC" if newest_first else "ASC")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_record(row) for row in rows]

    def latest(self, count: int = 1) -> list[WebhookRecord]:
        return self.list(limit=count, newest_first=True)

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])

    def prune_to(self, keep: int) -> int:
        """Keep only the ``keep`` newest requests; return how many were removed.

        Used by the capture server to bound the store: once the cap is reached
        every new capture evicts the oldest one. ``keep <= 0`` is a no-op, which
        is how "no retention limit" is spelled.
        """
        if keep <= 0:
            return 0
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM requests WHERE id NOT IN"
                " (SELECT id FROM requests ORDER BY id DESC LIMIT ?)",
                (keep,),
            )
            return int(cur.rowcount or 0)

    def prune_older_than(self, cutoff: str) -> int:
        """Delete requests received before the ISO timestamp ``cutoff``."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM requests WHERE received_at < ?", (cutoff,)
            )
            return int(cur.rowcount or 0)

    def clear(self) -> int:
        """Delete every stored request; return how many were removed."""
        removed = self.count()
        with self._connect() as conn:
            conn.execute("DELETE FROM requests")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'requests'")
        return removed


def _row_to_record(row: sqlite3.Row) -> WebhookRecord:
    headers = [tuple(pair) for pair in json.loads(row["headers"])]
    body = row["body"]
    if isinstance(body, str):  # defensive: older/binary rows
        body = body.encode("utf-8", "replace")
    return WebhookRecord(
        id=row["id"],
        method=row["method"],
        path=row["path"],
        headers=headers,
        body=bytes(body),
        remote_addr=row["remote_addr"],
        received_at=row["received_at"],
    )
