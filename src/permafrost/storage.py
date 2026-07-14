"""EdgeStore — the daemon's crash-safe SQLite (WAL) database.

Tables (SPEC §8 + COMPLEXITY):
- ``readings(id, ts, probe, temp_c, humidity_pct, door_open, power_ok)`` — 24h ring buffer
- ``events(id, ts, kind, payload)`` — door/power transitions, reflex firings…
- ``log_chain(seq, ts, entry, hash)`` — the tamper-evident hash chain
- ``rules(version, json, sig, source, activated_ts)`` — versioned signed rule bundles
- ``queue(id, created_ts, digest, sealed, synced, synced_ts)`` — ECIES-sealed offline event queue
- ``roots(day, merkle_root, sig, first_seq, last_seq, n, partial)`` — Ed25519-signed daily Merkle roots
- ``daily_stats(day, n, sum_temp, min_temp, max_temp)`` — survives ring-buffer eviction (multi-day trend rule)
- ``meta(key, value)`` — small daemon state (last heartbeat, …)

WAL + ``synchronous=NORMAL`` keeps every committed tick durable across a
process kill (invariant I1's transport).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .timeutil import day_of

__all__ = ["Reading", "EdgeStore", "RING_WINDOW_S"]

RING_WINDOW_S = 24 * 3600.0  # SPEC §5: "ring buffer 24h"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    probe TEXT NOT NULL DEFAULT 'cabinet',
    temp_c REAL NOT NULL,
    humidity_pct REAL,
    door_open INTEGER NOT NULL DEFAULT 0,
    power_ok INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS log_chain(
    seq INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    entry TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules(
    version INTEGER PRIMARY KEY,
    json TEXT NOT NULL,
    sig TEXT NOT NULL,
    source TEXT NOT NULL,
    activated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS queue(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts REAL NOT NULL,
    digest TEXT NOT NULL,
    sealed BLOB NOT NULL,
    synced INTEGER NOT NULL DEFAULT 0,
    synced_ts REAL
);
CREATE TABLE IF NOT EXISTS roots(
    day TEXT PRIMARY KEY,
    merkle_root TEXT NOT NULL,
    sig TEXT NOT NULL,
    first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL,
    n INTEGER NOT NULL,
    partial INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS daily_stats(
    day TEXT PRIMARY KEY,
    n INTEGER NOT NULL,
    sum_temp REAL NOT NULL,
    min_temp REAL NOT NULL,
    max_temp REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Reading:
    ts: float
    temp_c: float
    humidity_pct: float | None
    door_open: bool
    power_ok: bool
    probe: str = "cabinet"


class EdgeStore:
    """Thin, explicit wrapper over the daemon's SQLite database."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ misc

    def close(self) -> None:
        self.conn.close()

    def journal_mode(self) -> str:
        return self.conn.execute("PRAGMA journal_mode").fetchone()[0]

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------------ readings (ring buffer)

    def add_reading(self, r: Reading) -> None:
        self.conn.execute(
            "INSERT INTO readings(ts, probe, temp_c, humidity_pct, door_open, power_ok) VALUES(?,?,?,?,?,?)",
            (r.ts, r.probe, r.temp_c, r.humidity_pct, int(r.door_open), int(r.power_ok)),
        )
        self._bump_daily(r)

    def _bump_daily(self, r: Reading) -> None:
        day = day_of(r.ts)
        self.conn.execute(
            """
            INSERT INTO daily_stats(day, n, sum_temp, min_temp, max_temp)
            VALUES(?, 1, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                n = n + 1,
                sum_temp = sum_temp + excluded.sum_temp,
                min_temp = MIN(min_temp, excluded.min_temp),
                max_temp = MAX(max_temp, excluded.max_temp)
            """,
            (day, r.temp_c, r.temp_c, r.temp_c),
        )

    def evict_ring(self, now_ts: float, window_s: float = RING_WINDOW_S) -> int:
        """Drop readings older than the ring window. Returns rows evicted."""
        cur = self.conn.execute("DELETE FROM readings WHERE ts < ?", (now_ts - window_s,))
        return cur.rowcount

    def last_reading_ts(self) -> float | None:
        row = self.conn.execute("SELECT MAX(ts) FROM readings").fetchone()
        return row[0]

    def readings_since(self, since_ts: float) -> list[Reading]:
        rows = self.conn.execute(
            "SELECT ts, temp_c, humidity_pct, door_open, power_ok, probe FROM readings WHERE ts >= ? ORDER BY ts",
            (since_ts,),
        ).fetchall()
        return [Reading(ts=t, temp_c=c, humidity_pct=h, door_open=bool(d), power_ok=bool(p), probe=pr) for t, c, h, d, p, pr in rows]

    def reading_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

    def daily_means(self) -> list[tuple[str, float]]:
        rows = self.conn.execute(
            "SELECT day, sum_temp / n FROM daily_stats ORDER BY day"
        ).fetchall()
        return [(d, m) for d, m in rows]

    # ------------------------------------------------------------------ events

    def add_event(self, ts: float, kind: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO events(ts, kind, payload) VALUES(?,?,?)",
            (ts, kind, json.dumps(payload, sort_keys=True)),
        )

    def events_since(self, since_ts: float, kinds: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if kinds:
            marks = ",".join("?" for _ in kinds)
            rows = self.conn.execute(
                f"SELECT ts, kind, payload FROM events WHERE ts >= ? AND kind IN ({marks}) ORDER BY ts",
                (since_ts, *kinds),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT ts, kind, payload FROM events WHERE ts >= ? ORDER BY ts", (since_ts,)
            ).fetchall()
        return [{"ts": t, "kind": k, **json.loads(p)} for t, k, p in rows]

    # ------------------------------------------------------------------ rules

    def active_rules(self) -> tuple[int, str, str] | None:
        """(version, bundle_json, sig) of the highest activated bundle."""
        row = self.conn.execute(
            "SELECT version, json, sig FROM rules ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def store_rules(self, version: int, bundle_json: str, sig: str, source: str, ts: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO rules(version, json, sig, source, activated_ts) VALUES(?,?,?,?,?)",
            (version, bundle_json, sig, source, ts),
        )

    def rules_history(self) -> list[tuple[int, str]]:
        return self.conn.execute("SELECT version, source FROM rules ORDER BY version").fetchall()

    # ------------------------------------------------------------------ offline queue

    def enqueue(self, created_ts: float, digest: str, sealed: bytes) -> int:
        cur = self.conn.execute(
            "INSERT INTO queue(created_ts, digest, sealed) VALUES(?,?,?)",
            (created_ts, digest, sealed),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def pending_batches(self) -> list[tuple[int, str, bytes]]:
        rows = self.conn.execute(
            "SELECT id, digest, sealed FROM queue WHERE synced = 0 ORDER BY id"
        ).fetchall()
        return [(i, d, bytes(s)) for i, d, s in rows]

    def pending_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM queue WHERE synced = 0").fetchone()[0]

    def synced_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM queue WHERE synced = 1").fetchone()[0]

    def mark_synced(self, queue_id: int, ts: float) -> None:
        self.conn.execute("UPDATE queue SET synced = 1, synced_ts = ? WHERE id = ?", (ts, queue_id))

    # ------------------------------------------------------------------ commit

    def commit(self) -> None:
        self.conn.commit()
