"""Deterministic curve-feature extraction (shared by edge, cloud, FakeQwen, bench)."""

from __future__ import annotations

from permafrost.features import CurveFeatures, CurvePoint, _percentile, extract_features


def _pts(rows):
    """rows: (ts, temp, hum, door, power)."""
    return [
        CurvePoint(ts=ts, temp_c=t, humidity_pct=h, door_open=bool(d), power_ok=bool(p))
        for ts, t, h, d, p in rows
    ]


def test_empty_curve_returns_zeroed_features():
    f = extract_features([])
    assert isinstance(f, CurveFeatures) and f.duration_h == 0 and f.peak_delta_c == 0


def test_from_dict_coerces_types():
    p = CurvePoint.from_dict({"ts": "1", "temp_c": "4.5", "humidity_pct": "40", "door_open": 1, "power_ok": 0})
    assert p.ts == 1.0 and p.temp_c == 4.5 and p.humidity_pct == 40.0 and p.door_open and not p.power_ok


def test_from_dict_null_humidity():
    p = CurvePoint.from_dict({"ts": 0, "temp_c": 4, "humidity_pct": None})
    assert p.humidity_pct is None


def test_baseline_and_peak_delta():
    rows = [(i * 10, 4.0, 45.0, 0, 1) for i in range(10)] + [(100, 7.2, 45.0, 0, 1)]
    f = extract_features(_pts(rows))
    assert abs(f.baseline_c - 4.0) < 1e-9
    assert abs(f.peak_delta_c - 3.2) < 1e-9


def test_max_rise_detected():
    # +0.8C/min over 5 min
    rows = [(i * 10, 4.0 + 0.8 * (i * 10) / 60.0, 45.0, 0, 1) for i in range(31)]
    f = extract_features(_pts(rows))
    assert f.max_rise_c_per_min >= 0.75


def test_humidity_delta():
    rows = [(i * 10, 4.0, 45.0 + (30.0 if i > 10 else 0.0), 0, 1) for i in range(20)]
    f = extract_features(_pts(rows))
    assert f.humidity_delta >= 29.0


def test_door_open_flags_and_duration():
    rows = [(i * 10, 4.0, 45.0, 1 if 5 <= i < 15 else 0, 1) for i in range(20)]
    f = extract_features(_pts(rows))
    assert f.door_open_any and f.door_open_s > 0


def test_power_out_seen():
    rows = [(i * 10, 4.0, 45.0, 0, 0 if i == 5 else 1) for i in range(10)]
    f = extract_features(_pts(rows))
    assert f.power_out_seen


def test_gap_ratio_flags_true_outlier_not_uniform_stretch():
    uniform = [(i * 60, 4.0, 45.0, 0, 1) for i in range(20)]
    assert extract_features(_pts(uniform)).gap_ratio <= 2.0
    withgap = [(i * 10, 4.0, 45.0, 0, 1) for i in range(10)] + [(10_000, 4.0, 45.0, 0, 1)]
    assert extract_features(_pts(withgap)).gap_ratio > 5.0


def test_spike_episodes_counted():
    # two separated spikes
    rows = []
    for i in range(60):
        t = 4.0
        if 10 <= i < 15 or 40 <= i < 45:
            t = 7.0
        rows.append((i * 10, t, 45.0, 0, 1))
    assert extract_features(_pts(rows)).spike_episodes == 2


def test_multiday_drift_from_daily_means():
    rows = [(i * 3600, 4.0, 45.0, 0, 1) for i in range(4)]
    means = [("2026-01-05", 4.0), ("2026-01-06", 4.4), ("2026-01-07", 4.8), ("2026-01-08", 5.2)]
    f = extract_features(_pts(rows), means)
    assert f.drift_days == 4 and abs(f.drift_c_per_day - 0.4) < 1e-9


def test_drift_ignored_below_three_days():
    rows = [(i * 3600, 4.0, 45.0, 0, 1) for i in range(2)]
    f = extract_features(_pts(rows), [("2026-01-05", 4.0), ("2026-01-06", 6.0)])
    assert f.drift_c_per_day == 0.0


def test_extraction_is_deterministic():
    rows = [(i * 10, 4.0 + (i % 3) * 0.1, 45.0, i % 2, 1) for i in range(30)]
    a = extract_features(_pts(rows)).to_dict()
    b = extract_features(_pts(rows)).to_dict()
    assert a == b


def test_to_dict_rounds_floats():
    rows = [(i * 10, 4.0 + i / 7.0, 45.0, 0, 1) for i in range(10)]
    d = extract_features(_pts(rows)).to_dict()
    for v in d.values():
        if isinstance(v, float):
            assert round(v, 4) == v


def test_percentile_of_empty_list_is_zero():
    assert _percentile([], 0.5) == 0.0


def test_humidity_delta_is_zero_when_no_probe_fitted():
    # base BOM has no humidity sensor (README Status) — every point's humidity_pct is None.
    rows = [(i * 10, 4.0 + 0.1 * i, None, 0, 1) for i in range(10)]
    f = extract_features(_pts(rows))
    assert f.humidity_delta == 0.0
