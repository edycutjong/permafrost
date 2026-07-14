"""Deterministic curve-feature extraction.

Used in three places so edge, cloud and the offline FakeQwen all reason over
the *same* numbers:

1. the cloud prompt builder (features are presented to the live model),
2. FakeQwen's deterministic classifier (fixture verdict selection),
3. ``bench.py`` (confusion matrix over the seed curves).

The features are exactly the discriminators SEED_DATA engineered:
periodicity + flat humidity (defrost) vs humidity spike + door (door ajar) vs
multi-day drift (compressor) vs telemetry gap / mains-out (power loss).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

__all__ = ["CurvePoint", "CurveFeatures", "extract_features"]


@dataclass(frozen=True)
class CurvePoint:
    ts: float
    temp_c: float
    humidity_pct: float | None
    door_open: bool
    power_ok: bool

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CurvePoint":
        return cls(
            ts=float(d["ts"]),
            temp_c=float(d["temp_c"]),
            humidity_pct=None if d.get("humidity_pct") is None else float(d["humidity_pct"]),
            door_open=bool(d.get("door_open", False)),
            power_ok=bool(d.get("power_ok", True)),
        )


@dataclass(frozen=True)
class CurveFeatures:
    duration_h: float
    baseline_c: float
    peak_delta_c: float
    max_rise_c_per_min: float
    humidity_delta: float
    door_open_any: bool
    door_open_s: float
    power_out_seen: bool
    gap_max_s: float
    gap_ratio: float
    spike_episodes: int
    drift_c_per_day: float
    drift_days: int

    def to_dict(self) -> dict[str, Any]:
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in asdict(self).items()}


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def extract_features(
    points: Sequence[CurvePoint],
    daily_means: Sequence[tuple[str, float]] = (),
) -> CurveFeatures:
    """Pure, deterministic feature extraction over a curve window.

    ``daily_means`` (from the edge's daily_stats table, which outlives the 24h
    ring buffer) carries the multi-day signal a 6h excursion window cannot.
    """
    if not points:
        return CurveFeatures(0, 0, 0, 0, 0, False, 0, False, 0, 0.0, 0, 0.0, 0)

    temps = sorted(p.temp_c for p in points)
    baseline = _percentile(temps, 0.10)
    peak_delta = max(p.temp_c for p in points) - baseline

    hums = [p.humidity_pct for p in points if p.humidity_pct is not None]
    if hums:
        hums_sorted = sorted(hums)
        humidity_delta = max(hums) - _percentile(hums_sorted, 0.10)
    else:
        humidity_delta = 0.0

    duration_h = (points[-1].ts - points[0].ts) / 3600.0

    # steepest rise over ~5-minute windows
    max_rise = 0.0
    window_s = 300.0
    j = 0
    for i in range(len(points)):
        while points[i].ts - points[j].ts > window_s:
            j += 1
        dt_min = (points[i].ts - points[j].ts) / 60.0
        if dt_min >= 1.0:
            rise = (points[i].temp_c - points[j].temp_c) / dt_min
            max_rise = max(max_rise, rise)

    door_any = any(p.door_open for p in points)
    door_open_s = 0.0
    gap_max = 0.0
    dts: list[float] = []
    for prev, cur in zip(points, points[1:]):
        dt = cur.ts - prev.ts
        dts.append(dt)
        gap_max = max(gap_max, dt)
        if prev.door_open:
            door_open_s += dt

    # gap_ratio makes gap detection cadence-relative, so downsampled curves
    # (uniformly stretched dt) never fake a blackout — only a true outlier does.
    median_dt = sorted(dts)[len(dts) // 2] if dts else 0.0
    gap_ratio = (gap_max / median_dt) if median_dt > 0 else 0.0

    power_out = any(not p.power_ok for p in points)

    # spike episodes: contiguous stretches >= baseline + 1.5C
    episodes = 0
    in_episode = False
    for p in points:
        hot = p.temp_c >= baseline + 1.5
        if hot and not in_episode:
            episodes += 1
            in_episode = True
        elif not hot:
            in_episode = False

    # multi-day drift from daily means (needs >= 3 days)
    drift_days = len(daily_means)
    drift_per_day = 0.0
    if drift_days >= 3:
        first, last = daily_means[0][1], daily_means[-1][1]
        drift_per_day = (last - first) / (drift_days - 1)

    return CurveFeatures(
        duration_h=duration_h,
        baseline_c=baseline,
        peak_delta_c=peak_delta,
        max_rise_c_per_min=max_rise,
        humidity_delta=humidity_delta,
        door_open_any=door_any,
        door_open_s=door_open_s,
        power_out_seen=power_out,
        gap_max_s=gap_max,
        gap_ratio=gap_ratio,
        spike_episodes=episodes,
        drift_c_per_day=drift_per_day,
        drift_days=drift_days,
    )
