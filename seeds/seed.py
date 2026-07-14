#!/usr/bin/env python3
"""Deterministic seed-curve generator (SEED_DATA.md). Stdlib only.

Usage:
    python seeds/seed.py            # print SHA256 of each committed file
    python seeds/seed.py --regen    # rebuild everything byte-identically
    python seeds/seed.py --check    # regen to memory and diff against disk (exit 1 on drift)

Determinism: one ``random.Random`` per curve with a fixed seed and
integer-grid noise (``randint``), so regeneration is byte-identical on any
platform. Curves are engineered, not recorded — the defrost/door pair shares
the spike shape and differs exactly on the humidity + door + periodicity
signals the diagnosis must use (the "vocabulary gap" twin).

CSV schema: ``ts,temp_c,humidity_pct,door_open,power_ok`` — ``ts`` is seconds
offset from the virtual epoch (2026-01-05T00:00:00Z, a Monday in ISO week 2).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEADER = "ts,temp_c,humidity_pct,door_open,power_ok"

BASE_TEMP = 4.0
BASE_HUM = 45.0


def _row(ts: int, temp: float, hum: float, door: int, power: int) -> str:
    return f"{ts},{temp:.3f},{hum:.1f},{door},{power}"


def _tnoise(rng: random.Random) -> float:
    return rng.randint(-5, 5) / 100.0  # ±0.05 C on a fixed grid


def _hnoise(rng: random.Random) -> float:
    return rng.randint(-5, 5) / 10.0  # ±0.5 %RH on a fixed grid


def _defrost_delta(offset_in_cycle: float, rise_s: float = 360.0, hold_s: float = 120.0, fall_s: float = 360.0, peak: float = 3.2) -> float:
    """Sawtooth defrost bump: linear rise, hold, linear fall (14 min total)."""
    if offset_in_cycle < 0:
        return 0.0
    if offset_in_cycle < rise_s:
        return peak * (offset_in_cycle / rise_s)
    if offset_in_cycle < rise_s + hold_s:
        return peak
    if offset_in_cycle < rise_s + hold_s + fall_s:
        return peak * (1.0 - (offset_in_cycle - rise_s - hold_s) / fall_s)
    return 0.0


# --------------------------------------------------------------------------- curves


def gen_defrost_cycle() -> str:
    """24h, 10s cadence. +3.2C spike, 14 min, every 6h, humidity flat -> BENIGN twin."""
    rng = random.Random(4201)
    starts = [3 * 3600, 9 * 3600, 15 * 3600, 21 * 3600]
    lines = [HEADER]
    for ts in range(0, 24 * 3600, 10):
        delta = 0.0
        for s in starts:
            delta = max(delta, _defrost_delta(ts - s))
        temp = BASE_TEMP + delta + _tnoise(rng)
        hum = BASE_HUM + _hnoise(rng)
        lines.append(_row(ts, temp, hum, 0, 1))
    return "\n".join(lines) + "\n"


def gen_door_ajar() -> str:
    """6h, 10s cadence. Door open 20 min at t=5h: +0.8C/min to +3.4C, humidity spike -> CRITICAL."""
    rng = random.Random(4202)
    open_at, close_at = 18000, 19200  # 20 minutes ajar
    lines = [HEADER]
    for ts in range(0, 6 * 3600, 10):
        if ts < open_at:
            temp = BASE_TEMP + _tnoise(rng)
            hum = BASE_HUM + _hnoise(rng)
            door = 0
        elif ts < close_at:
            elapsed = ts - open_at
            temp = BASE_TEMP + min(3.4, 0.8 * (elapsed / 60.0)) + _tnoise(rng)
            hum = BASE_HUM + min(33.0, 33.0 * (elapsed / 120.0)) + _hnoise(rng)
            door = 1
        else:
            since = ts - close_at
            temp = BASE_TEMP + 3.4 * math.exp(-since / 1200.0) + _tnoise(rng)
            hum = BASE_HUM + 33.0 * math.exp(-since / 900.0) + _hnoise(rng)
            door = 0
        lines.append(_row(ts, temp, hum, door, 1))
    return "\n".join(lines) + "\n"


def gen_compressor_drift() -> str:
    """5 days, 60s cadence. Mean +0.4C/day, defrost pattern intact -> SERVICE flag."""
    rng = random.Random(4203)
    lines = [HEADER]
    for ts in range(0, 5 * 24 * 3600, 60):
        drift = 0.4 * (ts / 86400.0)
        cycle_offset = ts % (6 * 3600)
        delta = _defrost_delta(cycle_offset - 3 * 3600 if cycle_offset >= 3 * 3600 else -1.0)
        temp = BASE_TEMP + drift + delta + _tnoise(rng)
        hum = BASE_HUM + _hnoise(rng)
        lines.append(_row(ts, temp, hum, 0, 1))
    return "\n".join(lines) + "\n"


def gen_power_loss() -> str:
    """3h, 10s cadence. Mains out 60s, then a 40-min telemetry gap (device dark), then recovery."""
    rng = random.Random(4204)
    mains_out_at, gap_start, gap_end = 3600, 3660, 6060  # 2400s gap = 40 min
    lines = [HEADER]
    ts = 0
    while ts < 3 * 3600:
        if gap_start < ts < gap_end:
            ts += 10
            continue  # the Pi itself was dark: provable absence, not fabricated rows
        if ts < mains_out_at:
            temp, power = BASE_TEMP + _tnoise(rng), 1
        elif ts <= gap_start:
            temp, power = BASE_TEMP + _tnoise(rng), 0  # UPS holding, mains out
        else:
            since = ts - gap_end
            temp, power = BASE_TEMP + 2.5 * math.exp(-since / 600.0) + _tnoise(rng), 1
        hum = BASE_HUM + _hnoise(rng)
        lines.append(_row(ts, temp, hum, 0, power))
        ts += 10
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- fixtures


EXPECTED = {
    "door_ajar": {
        "cause": "door_ajar",
        "benign": False,
        "critical": True,
        "min_confidence": 0.85,
        "required_actions": ["sound_alarm", "notify"],
        "reflex_rules_expected": ["door_timer", "fast_rise"],
        "citation_required": True,
    },
    "defrost_cycle": {
        "cause": "defrost_cycle",
        "benign": True,
        "critical": False,
        "min_confidence": 0.90,
        "required_actions": ["annotate_log"],
        "reflex_rules_expected": ["fast_rise"],
        "citation_required": False,
    },
    "compressor_drift": {
        "cause": "compressor_degradation",
        "benign": False,
        "critical": False,
        "min_confidence": 0.80,
        "required_actions": ["schedule_service"],
        "reflex_rules_expected": ["slow_drift", "fast_rise"],
        "citation_required": False,
    },
    "power_loss": {
        "cause": "power_loss",
        "benign": False,
        "critical": True,
        "min_confidence": 0.85,
        "required_actions": ["notify"],
        "reflex_rules_expected": ["power_out", "sample_gap"],
        "citation_required": True,
    },
}

FRIDGE = {
    "fridge_id": "clinic-fridge-01",
    "model": "LabCool VR-240 (demo fixture)",
    "defrost_spec": "auto-defrost every 6h, approx 14 min, bounded +3.2C",
    "setpoint_c": 4.0,
    "band_c": [2.0, 8.0],
    "vfc_grade": "A",
    "contents_value_usd": 8000,
    "location": "two-room clinic (demo fixture; no real clinic data anywhere)",
}


def build_files() -> dict[str, str]:
    files: dict[str, str] = {
        "defrost_cycle.csv": gen_defrost_cycle(),
        "door_ajar.csv": gen_door_ajar(),
        "compressor_drift.csv": gen_compressor_drift(),
        "power_loss.csv": gen_power_loss(),
        "fridge.json": json.dumps(FRIDGE, indent=2, sort_keys=True) + "\n",
    }
    for name, expected in EXPECTED.items():
        files[f"{name}.expected.json"] = json.dumps(expected, indent=2, sort_keys=True) + "\n"
    return files


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regen", action="store_true", help="write all seed files")
    parser.add_argument("--check", action="store_true", help="verify disk matches the generator byte-for-byte")
    args = parser.parse_args(argv)

    files = build_files()
    if args.regen:
        for name, content in files.items():
            (HERE / name).write_text(content)
            print(f"wrote {name} ({len(content)} bytes)")
        return 0
    if args.check:
        drift = 0
        for name, content in files.items():
            on_disk = (HERE / name).read_bytes() if (HERE / name).exists() else b""
            status = "OK" if on_disk == content.encode() else "DRIFT"
            drift += status == "DRIFT"
            print(f"{status:5s} {name}")
        return 1 if drift else 0
    for name, content in files.items():
        print(f"{hashlib.sha256(content.encode()).hexdigest()}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
