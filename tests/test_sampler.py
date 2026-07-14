"""Sample sources: CsvSource (the replay path) + GpioSource's guarded hardware imports.

GpioSource is only ever exercised through its guarded ImportError paths here — real
w1thermsensor/gpiozero are never installed on a dev machine, and this suite must stay
fully offline, so we fake just enough of ``sys.modules`` to walk the second guarded
import without ever touching real hardware.
"""

from __future__ import annotations

import sys
import types

import pytest

from helpers import write_csv
from permafrost.sampler import CsvSource, GpioSource, HardwareUnavailable
from permafrost.timeutil import VIRTUAL_EPOCH_TS


def test_csv_source_reads_rows_in_ts_order(tmp_path):
    csv = write_csv(tmp_path / "c.csv", [(0, 4.0, 45.0, 0, 1), (10, 4.1, 46.0, 0, 1)])
    src = CsvSource(csv)
    r1, r2 = src.read(), src.read()
    assert r1.ts == VIRTUAL_EPOCH_TS and r1.temp_c == 4.0
    assert r2.ts == VIRTUAL_EPOCH_TS + 10 and r2.humidity_pct == 46.0
    assert src.read() is None  # exhausted


def test_csv_source_skip_until_resumes_after_a_kill(tmp_path):
    csv = write_csv(
        tmp_path / "c.csv", [(0, 4.0, 45.0, 0, 1), (10, 4.1, 45.0, 0, 1), (20, 4.2, 45.0, 0, 1)]
    )
    src = CsvSource(csv, skip_until_ts=VIRTUAL_EPOCH_TS + 10)
    r = src.read()
    assert r.ts == VIRTUAL_EPOCH_TS + 20  # rows at/before the skip point are gone
    assert src.read() is None


def test_gpio_source_raises_without_any_hardware_stack():
    with pytest.raises(HardwareUnavailable, match="hardware mode needs the Pi stack"):
        GpioSource()


def test_gpio_source_raises_when_only_gpiozero_is_present(monkeypatch):
    """gpiozero importable but w1thermsensor missing: the SECOND guarded import fails too."""
    fake_gpiozero = types.ModuleType("gpiozero")
    fake_gpiozero.Button = object  # never instantiated — we fail on the next import first
    monkeypatch.setitem(sys.modules, "gpiozero", fake_gpiozero)
    monkeypatch.delitem(sys.modules, "w1thermsensor", raising=False)
    with pytest.raises(HardwareUnavailable, match="hardware mode needs the Pi stack"):
        GpioSource()
