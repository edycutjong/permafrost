"""Virtual-clock helpers: the fixed epoch + day/week/human-readable rendering."""

from __future__ import annotations

from permafrost.timeutil import VIRTUAL_EPOCH_TS, day_of, iso_ts, iso_week_of


def test_virtual_epoch_is_monday_iso_week_2_of_2026():
    assert day_of(VIRTUAL_EPOCH_TS) == "2026-01-05"
    assert iso_week_of(VIRTUAL_EPOCH_TS) == 2


def test_day_of_rolls_over_at_midnight_utc():
    assert day_of(VIRTUAL_EPOCH_TS + 86399) == "2026-01-05"
    assert day_of(VIRTUAL_EPOCH_TS + 86400) == "2026-01-06"


def test_iso_week_of_advances_after_seven_days():
    assert iso_week_of(VIRTUAL_EPOCH_TS + 7 * 86400) == 3


def test_iso_ts_renders_human_readable_utc_timestamp():
    assert iso_ts(VIRTUAL_EPOCH_TS) == "2026-01-05 00:00:00"
    assert iso_ts(VIRTUAL_EPOCH_TS + 3661) == "2026-01-05 01:01:01"
