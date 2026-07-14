"""Replay harness: determinism, per-curve expected.json contracts, chain/roots health."""

from __future__ import annotations

import json
import sqlite3

import pytest

from helpers import SEEDS_DIR
from permafrost.qwen.fake import FakeQwen
from permafrost.replay import DEFAULT_FRIDGE_META, _load_fridge_meta, run_replay
from permafrost.storage import EdgeStore

CURVES = ["door_ajar", "defrost_cycle", "power_loss", "compressor_drift"]


def _head(db):
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT hash FROM log_chain ORDER BY seq DESC LIMIT 1").fetchone()
        n = conn.execute("SELECT COUNT(*) FROM log_chain").fetchone()[0]
        return (row[0] if row else None), n
    finally:
        conn.close()


def _expected(curve):
    return json.loads((SEEDS_DIR / f"{curve}.expected.json").read_text())


@pytest.mark.parametrize("curve", CURVES)
def test_replay_chain_and_roots_green(tmp_path, curve):
    r = run_replay(SEEDS_DIR / f"{curve}.csv", tmp_path / f"{curve}.db", transport=FakeQwen())
    assert r.chain_report.ok and r.chain_report.roots_ok
    assert r.roots_signed  # at least one signed daily Merkle root


def test_replay_is_byte_deterministic(tmp_path):
    run_replay(SEEDS_DIR / "door_ajar.csv", tmp_path / "a.db", transport=FakeQwen())
    run_replay(SEEDS_DIR / "door_ajar.csv", tmp_path / "b.db", transport=FakeQwen())
    assert _head(tmp_path / "a.db") == _head(tmp_path / "b.db")
    assert _head(tmp_path / "a.db")[0] is not None


def test_replay_determinism_holds_for_every_curve(tmp_path):
    for curve in CURVES:
        run_replay(SEEDS_DIR / f"{curve}.csv", tmp_path / f"{curve}-1.db", transport=FakeQwen())
        run_replay(SEEDS_DIR / f"{curve}.csv", tmp_path / f"{curve}-2.db", transport=FakeQwen())
        assert _head(tmp_path / f"{curve}-1.db") == _head(tmp_path / f"{curve}-2.db"), curve


def test_db_is_wal_mode(tmp_path):
    db = tmp_path / "w.db"
    run_replay(SEEDS_DIR / "door_ajar.csv", db, transport=FakeQwen())
    store = EdgeStore(db)
    try:
        assert store.journal_mode().lower() == "wal"
    finally:
        store.close()


# --------------------------------------------------------------------------- expected.json contracts

def test_door_ajar_expected(door_replay):
    result, db, _ = door_replay
    exp = _expected("door_ajar")
    causes = [e["verdict"]["cause"] for e in result.verdicts]
    assert exp["cause"] in causes
    door_v = next(e["verdict"] for e in result.verdicts if e["verdict"]["cause"] == "door_ajar")
    assert door_v["benign"] == exp["benign"]
    assert door_v["confidence"] >= exp["min_confidence"]
    tools = {a["tool"] for a in door_v["actions"]}
    assert set(exp["required_actions"]) <= tools
    fired = {f.rule_id for f in result.firings}
    assert set(exp["reflex_rules_expected"]) <= fired
    assert len(result.alarms) >= 1


def test_defrost_is_benign_no_alarm(defrost_replay):
    result, db, _ = defrost_replay
    assert all(e["verdict"]["cause"] == "defrost_cycle" for e in result.verdicts)
    assert all(e["verdict"]["benign"] for e in result.verdicts)
    assert len(result.alarms) == 0  # the anti-false-alarm beat: benign twin never sirens


def test_power_loss_expected(power_replay):
    result, db, _ = power_replay
    exp = _expected("power_loss")
    causes = [e["verdict"]["cause"] for e in result.verdicts]
    assert exp["cause"] in causes
    fired = {f.rule_id for f in result.firings}
    assert set(exp["reflex_rules_expected"]) <= fired


def test_compressor_drift_expected(compressor_replay):
    result, db, _ = compressor_replay
    exp = _expected("compressor_drift")
    causes = [e["verdict"]["cause"] for e in result.verdicts]
    assert exp["cause"] in causes  # service flag appears once drift accumulates >= 3 days
    drift_v = next(e["verdict"] for e in result.verdicts if e["verdict"]["cause"] == "compressor_degradation")
    tools = {a["tool"] for a in drift_v["actions"]}
    assert set(exp["required_actions"]) <= tools


def test_defrost_vs_door_the_one_devastating_query(defrost_replay, door_replay):
    """Two similar spikes: agent calls one benign, one critical (single-screen story)."""
    dresult, _, _ = defrost_replay
    oresult, _, _ = door_replay
    assert all(e["verdict"]["benign"] for e in dresult.verdicts)  # defrost: benign, no alarm
    assert len(dresult.alarms) == 0
    assert any(not e["verdict"]["benign"] for e in oresult.verdicts)  # door: critical
    assert len(oresult.alarms) >= 1


def test_every_verdict_revalidates_as_schema(door_replay, defrost_replay, power_replay, compressor_replay):
    from permafrost.verdict import ExcursionVerdict
    for fixture in (door_replay, defrost_replay, power_replay, compressor_replay):
        result, _, _ = fixture
        for env in result.verdicts:
            ExcursionVerdict.model_validate(env["verdict"])


def test_verdicts_carry_task_ids(door_replay):
    result, db, _ = door_replay
    assert all(env["task_id"].startswith("fake-") for env in result.verdicts)


# --------------------------------------------------------------------------- ReplayResult / meta helpers

def test_alarms_during_filters_by_window(door_replay):
    result, db, _ = door_replay
    assert result.alarms  # door_ajar always sirens at least once
    a = result.alarms[0]
    assert a in result.alarms_during((a.ts, a.ts))
    assert a not in result.alarms_during((a.ts - 100_000, a.ts - 50_000))


def test_load_fridge_meta_defaults_when_no_fridge_json_alongside_the_curve(tmp_path):
    curve = tmp_path / "curve.csv"
    curve.write_text("ts,temp_c,humidity_pct,door_open,power_ok\n0,4.0,45.0,0,1\n")
    assert _load_fridge_meta(curve) == DEFAULT_FRIDGE_META


# --------------------------------------------------------------------------- run_replay knobs

def test_tick_limit_stops_the_loop_early(tmp_path):
    result = run_replay(SEEDS_DIR / "door_ajar.csv", tmp_path / "lim.db", transport=FakeQwen(), tick_limit=5)
    assert result.ticks == 5


def test_on_tick_callback_runs_once_per_processed_tick(tmp_path):
    seen: list[tuple[int, float]] = []
    run_replay(
        SEEDS_DIR / "door_ajar.csv", tmp_path / "ot.db", transport=FakeQwen(), tick_limit=3,
        on_tick=lambda tick, daemon, tick_result: seen.append((tick, tick_result.ts)),
    )
    assert [t for t, _ in seen] == [1, 2, 3]


def test_throttle_ms_sleeps_between_ticks(tmp_path, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    run_replay(
        SEEDS_DIR / "door_ajar.csv", tmp_path / "th.db", transport=FakeQwen(),
        tick_limit=4, throttle_ms=5.0,
    )
    assert len(slept) == 4
    assert all(abs(s - 0.005) < 1e-9 for s in slept)


def test_reconnects_at_end_when_the_network_was_never_restored(tmp_path):
    """offline_from with no matching online_from: the curve ends offline, so
    run_replay's end-of-loop safety net must flip the link back on and flush."""
    result = run_replay(
        SEEDS_DIR / "door_ajar.csv", tmp_path / "off_end.db", transport=FakeQwen(), offline_from=1700,
    )
    assert result.offline_ticks > 0  # genuinely spent time offline
    assert result.pending_after == 0  # forced reconnect-at-end drained the queue
    assert result.chain_report.ok and result.chain_report.roots_ok
