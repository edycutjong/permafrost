"""The four named cold-chain invariants (COMPLEXITY §2), each as an explicit test.

I1  the hash chain verifies gap-free across a SIMULATED POWER CUT
    (a child process is hard-killed with os._exit mid-replay; the WAL survives,
    the daemon restarts and resumes, and the chain is still gap-free).
I2  any 1-byte log tamper fails verify-chain.
I3  the edge REFUSES an unsigned / invalid rule bundle before hot-swap.
I4  every CRITICAL verdict carries >= 1 guidance citation.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from helpers import SEEDS_DIR, flip_one_byte, sql
from permafrost.chain import verify_chain
from permafrost.crypto import dev_keys, generate_signing_seed
from permafrost.daemon import EdgeDaemon
from permafrost.qwen.fake import FakeQwen
from permafrost.replay import run_replay
from permafrost.rules import RuleBundleRejected, builtin_bundle_dict, sign_bundle
from permafrost.storage import EdgeStore
from permafrost.timeutil import VIRTUAL_EPOCH_TS as T0
from permafrost.verdict import is_critical_dict

CURVES = ["door_ajar", "defrost_cycle", "power_loss", "compressor_drift"]

# A child that samples N ticks, commits each, then HARD-kills itself (no cleanup,
# no WAL checkpoint) — the closest faithful stand-in for yanking the power.
_KILL_CHILD = textwrap.dedent(
    """
    import os, sys
    from permafrost.storage import EdgeStore
    from permafrost.daemon import EdgeDaemon
    from permafrost.sampler import CsvSource
    db, csv, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
    store = EdgeStore(db)
    daemon = EdgeDaemon(store)
    src = CsvSource(csv)
    i = 0
    while i < n:
        r = src.read()
        if r is None:
            break
        daemon.process_tick(r)   # commits every tick
        i += 1
    os._exit(9)                  # power cut: no store.close(), no checkpoint
    """
)


def _entries(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM log_chain").fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- I1

def test_I1_chain_gap_free_through_simulated_power_cut(tmp_path):
    child = tmp_path / "kill_child.py"
    child.write_text(_KILL_CHILD)
    db = tmp_path / "cut.db"
    curve = str(SEEDS_DIR / "door_ajar.csv")

    # --- power cut: kill the daemon after 1000 committed ticks ---
    proc = subprocess.run([sys.executable, str(child), str(db), curve, "1000"])
    assert proc.returncode == 9, "child must have been hard-killed, not exited cleanly"

    # WAL survived the kill: the chain is intact and gap-free right now
    mid = verify_chain(db, dev_keys().verify_key)
    assert mid.ok, f"post-kill chain broken: {mid.summary()}"
    entries_after_kill = mid.entries
    assert entries_after_kill > 0

    # --- restart: resume from the last committed reading ---
    resumed = run_replay(SEEDS_DIR / "door_ajar.csv", db, transport=FakeQwen(), resume=True)
    assert resumed.chain_report.ok and resumed.chain_report.roots_ok
    # the chain grew past the kill point and still protects (door alarm fired)
    assert resumed.chain_report.entries > entries_after_kill
    assert len(resumed.alarms) >= 1


def test_I1_resumed_chain_covers_full_curve(tmp_path):
    child = tmp_path / "kill_child.py"
    child.write_text(_KILL_CHILD)
    db = tmp_path / "cut.db"
    curve = str(SEEDS_DIR / "door_ajar.csv")
    subprocess.run([sys.executable, str(child), str(db), curve, "500"], check=False)
    run_replay(SEEDS_DIR / "door_ajar.csv", db, transport=FakeQwen(), resume=True)

    # compare against an uninterrupted run: same reading coverage, chain verifies
    ref_db = tmp_path / "ref.db"
    run_replay(SEEDS_DIR / "door_ajar.csv", ref_db, transport=FakeQwen())
    n_readings_resumed = sql(db, "SELECT COUNT(*) FROM readings")[0][0]
    n_readings_ref = sql(ref_db, "SELECT COUNT(*) FROM readings")[0][0]
    assert n_readings_resumed == n_readings_ref
    assert verify_chain(db, dev_keys().verify_key).ok


def test_I1_committed_tick_is_durable_without_close(tmp_path):
    """A committed tick is readable by a second connection before the writer closes."""
    db = tmp_path / "d.db"
    store = EdgeStore(db)
    daemon = EdgeDaemon(store)
    from helpers import R
    for i in range(20):
        daemon.process_tick(R(i * 10))
    # do NOT close store; open an independent connection and verify
    assert verify_chain(db, dev_keys().verify_key).ok
    assert _entries(db) >= 20
    store.close()


# --------------------------------------------------------------------------- I2

@pytest.mark.parametrize("curve", CURVES)
def test_I2_one_byte_entry_tamper_fails(tmp_path, curve):
    db = tmp_path / f"{curve}.db"
    run_replay(SEEDS_DIR / f"{curve}.csv", db, transport=FakeQwen())
    assert verify_chain(db, dev_keys().verify_key).ok  # green before tamper
    mid = _entries(db) // 2
    (entry,) = sql(db, "SELECT entry FROM log_chain WHERE seq=?", (mid,))[0]
    sql(db, "UPDATE log_chain SET entry=? WHERE seq=?", (flip_one_byte(entry), mid))
    report = verify_chain(db, dev_keys().verify_key)
    assert not report.ok and report.first_bad_seq == mid


def test_I2_one_byte_hash_tamper_fails(tmp_path):
    db = tmp_path / "door.db"
    run_replay(SEEDS_DIR / "door_ajar.csv", db, transport=FakeQwen())
    (h,) = sql(db, "SELECT hash FROM log_chain WHERE seq=10")[0]
    sql(db, "UPDATE log_chain SET hash=? WHERE seq=10", (flip_one_byte(h),))
    assert not verify_chain(db, dev_keys().verify_key).ok


def test_I2_signed_root_tamper_fails(tmp_path):
    db = tmp_path / "door.db"
    run_replay(SEEDS_DIR / "door_ajar.csv", db, transport=FakeQwen())
    (root,) = sql(db, "SELECT merkle_root FROM roots LIMIT 1")[0]
    sql(db, "UPDATE roots SET merkle_root=? WHERE merkle_root=?", (flip_one_byte(root), root))
    report = verify_chain(db, dev_keys().verify_key)
    assert not report.roots_ok  # chain fine, signed daily root poisoned


# --------------------------------------------------------------------------- I3

def _v2():
    b = builtin_bundle_dict()
    b["version"] = 2
    b["source"] = "distilled"
    return b


def test_I3_edge_refuses_unsigned_bundle(tmp_path):
    store = EdgeStore(tmp_path / "d.db")
    daemon = EdgeDaemon(store)
    try:
        with pytest.raises(RuleBundleRejected):
            daemon.try_activate_bundle(_v2(), None, T0)
        assert daemon.rules_version == 1  # never hot-swapped
        kinds = [json.loads(e)["kind"] for (e,) in sql(tmp_path / "d.db", "SELECT entry FROM log_chain ORDER BY seq")]
        assert "rule_bundle_rejected" in kinds  # refusal is itself chain-logged
    finally:
        store.close()


def test_I3_edge_refuses_forged_signature(tmp_path):
    store = EdgeStore(tmp_path / "d.db")
    daemon = EdgeDaemon(store)
    try:
        forged = sign_bundle(_v2(), generate_signing_seed())
        with pytest.raises(RuleBundleRejected):
            daemon.try_activate_bundle(_v2(), forged, T0)
        assert daemon.rules_version == 1
    finally:
        store.close()


def test_I3_valid_signature_is_accepted(tmp_path):
    store = EdgeStore(tmp_path / "d.db")
    daemon = EdgeDaemon(store)
    try:
        good = sign_bundle(_v2(), dev_keys().signing_seed)
        assert daemon.try_activate_bundle(_v2(), good, T0) is True
        assert daemon.rules_version == 2
    finally:
        store.close()


# --------------------------------------------------------------------------- I4

@pytest.mark.parametrize("curve", CURVES)
def test_I4_every_critical_verdict_cites_guidance(tmp_path, curve):
    result = run_replay(SEEDS_DIR / f"{curve}.csv", tmp_path / f"{curve}.db", transport=FakeQwen())
    crit = [e["verdict"] for e in result.verdicts if is_critical_dict(e["verdict"])]
    for v in crit:
        assert v["guidance_citation"].strip(), f"{curve}: critical verdict without citation"


def test_I4_door_ajar_produces_a_cited_critical(door_replay):
    result, _, _ = door_replay
    crit = [e["verdict"] for e in result.verdicts if is_critical_dict(e["verdict"])]
    assert crit, "door-ajar must produce at least one critical verdict"
    assert all(v["guidance_citation"].strip() for v in crit)


def test_I4_schema_enforces_citation_at_construction():
    from pydantic import ValidationError
    from permafrost.verdict import ExcursionVerdict
    with pytest.raises(ValidationError, match="I4"):
        ExcursionVerdict.model_validate({
            "cause": "door_ajar", "confidence": 0.9, "benign": False,
            "evidence": ["x"], "guidance_citation": "",
            "actions": [{"tool": "sound_alarm", "now": True}],
        })
