"""Shared test helpers: synthetic curves, tiny CSVs, db tamper utilities."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from permafrost.storage import Reading
from permafrost.timeutil import VIRTUAL_EPOCH_TS

BUILD_DIR = Path(__file__).resolve().parents[1]
SEEDS_DIR = BUILD_DIR / "seeds"


def R(offset_s: float, temp: float = 4.0, hum: float | None = 45.0, door: bool = False, power: bool = True) -> Reading:
    """Reading at a virtual-clock offset."""
    return Reading(ts=VIRTUAL_EPOCH_TS + offset_s, temp_c=temp, humidity_pct=hum, door_open=door, power_ok=power)


def series(n: int, *, start_s: float = 0.0, step_s: float = 10.0, temp: float = 4.0) -> list[Reading]:
    return [R(start_s + i * step_s, temp=temp) for i in range(n)]


def write_csv(path: Path, rows: list[tuple[int, float, float, int, int]]) -> Path:
    """rows: (ts_offset, temp, humidity, door, power)."""
    lines = ["ts,temp_c,humidity_pct,door_open,power_ok"]
    lines += [f"{ts},{t:.3f},{h:.1f},{d},{p}" for ts, t, h, d, p in rows]
    path.write_text("\n".join(lines) + "\n")
    return path


def tiny_door_csv(path: Path) -> Path:
    """~7 min curve: 2 min calm, then door open with fast rise (fires door_timer + fast_rise)."""
    rows: list[tuple[int, float, float, int, int]] = []
    for i in range(12):  # 2 min calm
        rows.append((i * 10, 4.0, 45.0, 0, 1))
    for i in range(30):  # 5 min door open, +0.8C/min
        ts = 120 + i * 10
        rows.append((ts, 4.0 + min(3.4, 0.8 * (i * 10) / 60.0), min(78.0, 45.0 + 33.0 * i / 12.0), 1, 1))
    return write_csv(path, rows)


def copy_db(src: Path, dst: Path) -> Path:
    """Copy an audit db (post-close, WAL checkpointed; copy sidecars if present)."""
    shutil.copy(src, dst)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.exists():
            shutil.copy(side, Path(str(dst) + suffix))
    return dst


def sql(db: Path, statement: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(statement, params)
        rows = cur.fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def flip_one_byte(text: str) -> str:
    """Deterministically flip one character mid-string (digit-safe swap)."""
    mid = len(text) // 2
    ch = text[mid]
    repl = "0" if ch != "0" else "1"
    return text[:mid] + repl + text[mid + 1 :]
