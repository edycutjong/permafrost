"""Virtual-clock helpers.

Replay mode never touches the wall clock: every timestamp is
``VIRTUAL_EPOCH + csv_offset_seconds``, which makes chain entries, Merkle
day-grouping and weekly reports byte-deterministic across runs.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["VIRTUAL_EPOCH", "VIRTUAL_EPOCH_TS", "day_of", "iso_week_of", "iso_ts"]

# Monday 2026-01-05 00:00:00 UTC — ISO week 2 of 2026. Fixed forever.
VIRTUAL_EPOCH = datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)
VIRTUAL_EPOCH_TS = VIRTUAL_EPOCH.timestamp()


def day_of(ts: float) -> str:
    """UTC calendar day (``YYYY-MM-DD``) for an epoch timestamp."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def iso_week_of(ts: float) -> int:
    """ISO week number for an epoch timestamp."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isocalendar().week


def iso_ts(ts: float) -> str:
    """Human-readable UTC timestamp."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
