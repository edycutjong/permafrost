"""bench — the numbers behind the claims (SPEC §10, COMPLEXITY §5).

1. 4-class confusion matrix x N runs (deterministic FakeQwen -> floor 0.9)
2. local reflex latency p50/p95 (<100 ms budget; in practice sub-ms)
3. cloud RTT p50/p95 through the in-process app (FC RTT pending deployment)
4. $/day before vs after distillation — measured from actual replays of the
   24h defrost curve, not asserted: the distilled recognizer resolves benign
   defrost spikes locally, and the door curve proves detection is NOT lost.

Prices are ESTIMATED public list prices (USD per 1K tokens) — constants below,
clearly marked; swap in billed numbers before quoting externally.
"""

from __future__ import annotations

import base64
import statistics
import time
from pathlib import Path
from typing import Any

from .cloud.app import create_app
from .cloud.diagnose import DiagnoseRequest, diagnose
from .cloud.guidance import GuidanceStore
from .qwen.fake import FakeQwen
from .rules import ReflexEngine, RuleBundle, builtin_bundle_dict
from .sampler import CsvSource
from .storage import Reading
from .timeutil import day_of

__all__ = ["ESTIMATED_PRICES_USD_PER_1K", "run_confusion_matrix", "run_latency", "run_cost_curve", "run_all"]

# ESTIMATES ONLY (public list-price ballpark; update with billed numbers).
ESTIMATED_PRICES_USD_PER_1K = {
    "qwen3.7-plus": {"in": 0.0004, "out": 0.0012},
    "qwen3.6-flash": {"in": 0.00005, "out": 0.00015},
    "text-embedding-v4": {"in": 0.00007, "out": 0.0},
    "qwen3-tts-instruct-flash": {"in": 0.0001, "out": 0.0},
}

SEED_CLASSES = ("door_ajar", "defrost_cycle", "compressor_drift", "power_loss")
CLASS_TO_CAUSE = {
    "door_ajar": "door_ajar",
    "defrost_cycle": "defrost_cycle",
    "compressor_drift": "compressor_degradation",
    "power_loss": "power_loss",
}


def _curve_rows(csv_path: Path) -> list[Reading]:
    source = CsvSource(csv_path)
    rows: list[Reading] = []
    while (r := source.read()) is not None:
        rows.append(r)
    return rows


def _daily_means(rows: list[Reading]) -> list[list[Any]]:
    acc: dict[str, tuple[int, float]] = {}
    for r in rows:
        day = day_of(r.ts)
        n, s = acc.get(day, (0, 0.0))
        acc[day] = (n + 1, s + r.temp_c)
    return [[day, s / n] for day, (n, s) in sorted(acc.items())]


def _request_for(csv_path: Path, max_points: int = 360) -> DiagnoseRequest:
    rows = _curve_rows(csv_path)
    stride = max(1, -(-len(rows) // max_points))
    picked = rows[::stride]
    curve = [
        {"ts": r.ts, "temp_c": r.temp_c, "humidity_pct": r.humidity_pct, "door_open": r.door_open, "power_ok": r.power_ok}
        for r in picked
    ]
    return DiagnoseRequest(
        fridge_meta={"fridge_id": "bench"},
        curve=curve,
        daily_means=_daily_means(rows),
        trigger={"rule_id": "bench", "severity": "watch", "message": "bench", "ts": rows[-1].ts},
    )


# --------------------------------------------------------------------------- 1. confusion matrix


def run_confusion_matrix(seeds_dir: Path, runs: int = 10) -> dict[str, Any]:
    transport = FakeQwen()
    store = GuidanceStore(transport)
    matrix: dict[str, dict[str, int]] = {c: {} for c in SEED_CLASSES}
    correct = total = 0
    for seed_class in SEED_CLASSES:
        req = _request_for(seeds_dir / f"{seed_class}.csv")
        expected = CLASS_TO_CAUSE[seed_class]
        for _ in range(runs):
            verdict = diagnose(req, transport, store).verdict
            predicted = verdict["cause"]
            matrix[seed_class][predicted] = matrix[seed_class].get(predicted, 0) + 1
            correct += int(predicted == expected)
            total += 1
    return {"matrix": matrix, "accuracy": correct / total, "runs": runs, "classes": len(SEED_CLASSES)}


# --------------------------------------------------------------------------- 2 + 3. latency


def run_latency(seeds_dir: Path, iterations: int = 200) -> dict[str, Any]:
    rows = _curve_rows(seeds_dir / "door_ajar.csv")
    window = [r for r in rows if r.ts >= rows[-1].ts - 1800.0]
    engine = ReflexEngine(RuleBundle.parse(builtin_bundle_dict()))
    daily = [(d, m) for d, m in ((x[0], x[1]) for x in _daily_means(rows))]

    reflex_ms: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        engine.evaluate(window[:-1], window[-1], daily, window[-2].ts)
        reflex_ms.append((time.perf_counter() - start) * 1000.0)
        engine._state.clear()  # measure cold evaluation each pass

    app = create_app(FakeQwen())
    from .canonical import canonical_json
    from .crypto import dev_keys, seal
    from .link import make_inprocess_client

    keys = dev_keys()
    client = make_inprocess_client(app, base_url="http://bench.local")
    req = _request_for(seeds_dir / "door_ajar.csv")
    sealed = seal(canonical_json(req.model_dump()), keys.sealing_public)
    rtt_ms: list[float] = []
    for _ in range(max(5, iterations // 20)):
        start = time.perf_counter()
        resp = client.post("/diagnose", json={"sealed_b64": base64.b64encode(sealed).decode()})
        resp.raise_for_status()
        rtt_ms.append((time.perf_counter() - start) * 1000.0)
    client.close()

    def pct(vals: list[float], q: float) -> float:
        return statistics.quantiles(vals, n=100)[int(q * 100) - 1] if len(vals) >= 2 else vals[0]

    return {
        "reflex_p50_ms": statistics.median(reflex_ms),
        "reflex_p95_ms": pct(reflex_ms, 0.95),
        "cloud_rtt_p50_ms": statistics.median(rtt_ms),
        "cloud_rtt_p95_ms": pct(rtt_ms, 0.95),
        "iterations": iterations,
    }


# --------------------------------------------------------------------------- 4. $/day pre/post distill


def _usd(usage_totals: dict[str, dict[str, int]]) -> float:
    cost = 0.0
    for model, t in usage_totals.items():
        price = ESTIMATED_PRICES_USD_PER_1K.get(model, {"in": 0.0, "out": 0.0})
        cost += t["prompt_tokens"] / 1000.0 * price["in"]
        cost += t["completion_tokens"] / 1000.0 * price["out"]
    return cost


def run_cost_curve(seeds_dir: Path, workdir: Path) -> dict[str, Any]:
    """Measured economics: replay 24h defrost pre/post distillation + door-day control."""
    from .cloud.distill import DistillRequest, distill
    from .crypto import dev_keys
    from .replay import run_replay
    from .reporting import verdict_history

    keys = dev_keys()
    workdir.mkdir(parents=True, exist_ok=True)

    # PRE: rules v1 — every defrost spike escalates to the cloud
    pre_transport = FakeQwen()
    pre_db = workdir / "cost_pre.db"
    pre_db.unlink(missing_ok=True)
    pre = run_replay(seeds_dir / "defrost_cycle.csv", pre_db, transport=pre_transport)
    pre_calls = len(pre.verdicts)
    pre_usd = _usd(pre_transport.usage.totals())

    # DISTILL: cloud compiles + signs what it learned from this fridge's history
    history = verdict_history(pre_db)
    distill_transport = FakeQwen()
    distilled = distill(
        DistillRequest(verdicts=history, current_version=1, now_ts=pre.last_ts or 0.0),
        distill_transport,
        keys.signing_seed,
    )
    distill_usd = _usd(distill_transport.usage.totals())

    # POST: rules v2 — the defrost recognizer resolves benign spikes locally
    post_transport = FakeQwen()
    post_db = workdir / "cost_post.db"
    post_db.unlink(missing_ok=True)
    post = run_replay(
        seeds_dir / "defrost_cycle.csv",
        post_db,
        transport=post_transport,
        initial_bundle=(distilled.bundle, distilled.sig),
    )
    post_calls = len(post.verdicts)
    post_usd = _usd(post_transport.usage.totals())

    # CONTROL: door-ajar day must still alarm + diagnose under v2 (no lost detection)
    ctrl_transport = FakeQwen()
    ctrl_db = workdir / "cost_ctrl.db"
    ctrl_db.unlink(missing_ok=True)
    ctrl = run_replay(
        seeds_dir / "door_ajar.csv",
        ctrl_db,
        transport=ctrl_transport,
        initial_bundle=(distilled.bundle, distilled.sig),
    )
    detection_kept = len(ctrl.alarms) > 0 and any(
        e.get("verdict", {}).get("cause") == "door_ajar" for e in ctrl.verdicts
    )

    savings = 1.0 - (post_usd / pre_usd) if pre_usd > 0 else 0.0
    return {
        "pre_calls": pre_calls,
        "post_calls": post_calls,
        "pre_usd_day": pre_usd,
        "post_usd_day": post_usd,
        "distill_usd": distill_usd,
        "savings_pct": savings * 100.0,
        "rules_version_post": post.rules_version,
        "detection_kept": detection_kept,
    }


# --------------------------------------------------------------------------- all together -> markdown


def run_all(seeds_dir: str | Path, workdir: str | Path, *, quick: bool = False) -> tuple[str, bool, dict[str, Any]]:
    seeds = Path(seeds_dir)
    work = Path(workdir)
    runs = 3 if quick else 10
    iterations = 50 if quick else 200

    cm = run_confusion_matrix(seeds, runs=runs)
    lat = run_latency(seeds, iterations=iterations)
    cost = run_cost_curve(seeds, work)

    ok = (
        cm["accuracy"] >= 0.9
        and lat["reflex_p95_ms"] < 100.0
        and cost["savings_pct"] >= 60.0
        and cost["detection_kept"]
    )

    causes = sorted({c for row in cm["matrix"].values() for c in row} | set(CLASS_TO_CAUSE.values()))
    lines = [
        "# Permafrost bench",
        "",
        "- Verdict source: FakeQwen (deterministic, offline). Live-model matrix: run with `PERMAFROST_LIVE=1`.",
        f"- Confusion matrix: {cm['classes']} classes x {cm['runs']} runs — **accuracy {cm['accuracy']:.3f}** (floor 0.9)",
        "",
        "## Cause classification (rows = truth, cols = predicted)",
        "",
        "| truth \\ predicted | " + " | ".join(causes) + " |",
        "|---|" + "|".join(["---"] * len(causes)) + "|",
    ]
    for seed_class in SEED_CLASSES:
        row = cm["matrix"][seed_class]
        lines.append(
            f"| {seed_class} | " + " | ".join(str(row.get(c, 0)) for c in causes) + " |"
        )
    lines += [
        "",
        "## Latency",
        "",
        "| tier | p50 | p95 | budget |",
        "|---|---|---|---|",
        f"| local reflex (rules engine) | {lat['reflex_p50_ms']:.3f} ms | {lat['reflex_p95_ms']:.3f} ms | < 100 ms |",
        f"| cloud round-trip (in-process app*) | {lat['cloud_rtt_p50_ms']:.1f} ms | {lat['cloud_rtt_p95_ms']:.1f} ms | n/a |",
        "",
        "\\* in-process ASGI — real FC RTT pending deployment (see README Status).",
        "",
        "## $/day — before vs after distillation (measured, estimated list prices)",
        "",
        "| phase | cloud diagnose calls / day | est. $/day |",
        "|---|---|---|",
        f"| pre-distill (rules v1) | {cost['pre_calls']} | ${cost['pre_usd_day']:.4f} |",
        f"| post-distill (rules v{cost['rules_version_post']}) | {cost['post_calls']} | ${cost['post_usd_day']:.4f} |",
        "",
        f"- one-off distillation cost: ${cost['distill_usd']:.5f} (qwen3.6-flash)",
        f"- **cloud spend savings: {cost['savings_pct']:.0f}%** (target >= 60%)",
        f"- detection preserved on door-ajar control day: **{'YES' if cost['detection_kept'] else 'NO'}** "
        "(alarm fired + door_ajar verdict under distilled rules)",
        "",
        f"**Overall: {'PASS' if ok else 'FAIL'}**",
    ]
    return "\n".join(lines), ok, {"confusion": cm, "latency": lat, "cost": cost}
