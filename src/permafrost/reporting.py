"""Edge-side weekly report: mined from the audit db's hash chain."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .chain import verify_chain
from .cloud.report import weekly_report_markdown
from .crypto import dev_keys
from .storage import EdgeStore
from .timeutil import iso_week_of

__all__ = ["chain_entries", "verdict_history", "edge_weekly_report"]


def chain_entries(db_path: str | Path) -> Iterator[dict[str, Any]]:
    store = EdgeStore(db_path)
    try:
        for (entry_str,) in store.conn.execute("SELECT entry FROM log_chain ORDER BY seq"):
            yield json.loads(entry_str)
    finally:
        store.close()


def verdict_history(db_path: str | Path) -> list[dict[str, Any]]:
    """Verdicts as flat dicts (verdict fields + ts + task_id) in chain order."""
    out: list[dict[str, Any]] = []
    for entry in chain_entries(db_path):
        if entry["kind"] == "verdict":
            row = dict(entry["payload"].get("verdict", {}))
            row["ts"] = entry["ts"]
            row["task_id"] = entry["payload"].get("task_id", "")
            out.append(row)
    return out


def edge_weekly_report(db_path: str | Path, week: int, verify_key_hex: str | None = None) -> str:
    verify_key = verify_key_hex or dev_keys().verify_key
    readings = 0
    in_band = 0
    fridge_id = "clinic-fridge-01"
    verdicts: list[dict[str, Any]] = []
    for entry in chain_entries(db_path):
        if iso_week_of(entry["ts"]) != week:
            continue
        if entry["kind"] == "reading":
            readings += 1
            t = entry["payload"]["temp_c"]
            if 2.0 <= t <= 8.0:
                in_band += 1
        elif entry["kind"] == "verdict":
            row = dict(entry["payload"].get("verdict", {}))
            row["ts"] = entry["ts"]
            row["task_id"] = entry["payload"].get("task_id", "")
            verdicts.append(row)

    report = verify_chain(db_path, verify_key)
    store = EdgeStore(db_path)
    try:
        roots = store.conn.execute("SELECT COUNT(*) FROM roots").fetchone()[0]
    finally:
        store.close()

    return weekly_report_markdown(
        week,
        verdicts,
        fridge_id=fridge_id,
        readings=readings,
        in_band_pct=(100.0 * in_band / readings) if readings else None,
        chain_summary=report.summary(),
        roots_signed=roots,
    )
