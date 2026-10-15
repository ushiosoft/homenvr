"""Event store (SQLite): motion/activity events for the smart timeline.

One small DB shared by the motion detectors (writers) and the API (reader).
WAL mode so a reader never blocks the writers. A connection is opened per
operation — cheap, and avoids cross-thread sharing issues with asyncio.to_thread.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class EventStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        return c

    def _init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "camera TEXT NOT NULL,"
                "ts REAL NOT NULL,"
                "type TEXT NOT NULL DEFAULT 'motion',"
                "score REAL DEFAULT 0,"
                "label TEXT DEFAULT '')"
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_cam_ts ON events(camera, ts)")
            # Migrate older DBs that predate the label column.
            cols = {r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()}
            if "label" not in cols:
                c.execute("ALTER TABLE events ADD COLUMN label TEXT DEFAULT ''")

    def add(self, camera: str, ts: float, type: str = "motion", score: float = 0.0) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO events(camera, ts, type, score) VALUES (?,?,?,?)",
                            (camera, ts, type, score))
            return cur.lastrowid

    def set_label(self, event_id: int, label: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE events SET label=? WHERE id=?", (label, event_id))

    def query(self, camera: str, start: float, end: float, limit: int = 5000) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, type, score, label FROM events WHERE camera=? AND ts>=? AND ts<? "
                "ORDER BY ts LIMIT ?", (camera, start, end, limit),
            ).fetchall()
        return [{"ts": r[0], "type": r[1], "score": r[2], "label": r[3] or ""} for r in rows]

    def latest(self, camera: str) -> float | None:
        with self._conn() as c:
            r = c.execute("SELECT MAX(ts) FROM events WHERE camera=?", (camera,)).fetchone()
        return r[0] if r and r[0] else None

    def prune(self, before: float) -> int:
        """Delete events older than `before` (keeps the DB bounded with retention)."""
        with self._conn() as c:
            cur = c.execute("DELETE FROM events WHERE ts < ?", (before,))
            return cur.rowcount


def now() -> float:
    return time.time()
