"""SQLite log of received requests and their outcome."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

PAYLOAD_MAX = 64 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    source TEXT,
    endpoint TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT,
    message TEXT,
    detail TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
"""


class EventStore:
    def __init__(self, path: str, store_payloads: bool = True):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.store_payloads = store_payloads
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")

    def add(
        self,
        *,
        endpoint: str,
        status: str,
        source: str | None = None,
        title: str | None = None,
        message: str | None = None,
        detail: str | None = None,
        payload: bytes | None = None,
    ) -> None:
        stored = None
        if self.store_payloads and payload:
            stored = payload[:PAYLOAD_MAX].decode("utf-8", errors="replace")
        with self._lock:
            self._db.execute(
                "INSERT INTO events (ts, source, endpoint, status, title, message, detail, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), source, endpoint, status, title, message, detail, stored),
            )
            self._db.commit()

    def recent(
        self, limit: int = 50, source: str | None = None, with_payload: bool = False
    ) -> list[dict[str, Any]]:
        cols = "id, ts, source, endpoint, status, title, message, detail" + (
            ", payload" if with_payload else ""
        )
        sql = f"SELECT {cols} FROM events"  # noqa: S608 - column list is static
        args: list[Any] = []
        if source:
            sql += " WHERE source = ?"
            args.append(source)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(500, limit)))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if with_payload and item.get("payload"):
                try:
                    item["payload"] = json.loads(item["payload"])
                except ValueError:
                    pass
            result.append(item)
        return result

    def prune(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            cur = self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._db.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()
