"""EdgeDaemon per-tick loop + the I3 signed-rule hot-swap gate."""

from __future__ import annotations

import json

import pytest

from helpers import R, sql, tiny_door_csv
from permafrost.cloud.app import create_app
from permafrost.crypto import dev_keys, generate_signing_seed
from permafrost.daemon import DaemonConfig, EdgeDaemon, activate_bundle_on_store
from permafrost.link import DiagnoserClient, LocalAppLink
from permafrost.qwen.fake import FakeQwen
from permafrost.qwen.transport import ChatResult, MODEL_DIAGNOSIS
from permafrost.rules import RuleBundleRejected, builtin_bundle_dict, sign_bundle
from permafrost.sampler import CsvSource
from permafrost.storage import EdgeStore
from permafrost.timeutil import VIRTUAL_EPOCH_TS as T0


def _chain_kinds(db):
    return [json.loads(e)["kind"] for (e,) in sql(db, "SELECT entry FROM log_chain ORDER BY seq")]


def _v2_bundle():
    b = builtin_bundle_dict()
    b["version"] = 2
    b["source"] = "distilled"
    return b


@pytest.fixture()
def daemon(tmp_path):
    store = EdgeStore(tmp_path / "d.db")
    d = EdgeDaemon(store)
    yield d, store, tmp_path / "d.db"
    store.close()


def test_fresh_daemon_provisions_builtin_v1(daemon):
    d, store, db = daemon
    assert d.rules_version == 1
    assert store.active_rules()[0] == 1
    assert "rules_activated" in _chain_kinds(db)


def test_tick_logs_reading_to_chain(daemon):
    d, store, db = daemon
    d.process_tick(R(0, temp=4.0))
    kinds = _chain_kinds(db)
    assert "reading" in kinds


def test_gap_detected_and_chained(daemon):
    d, store, db = daemon
    d.process_tick(R(0))
    d.process_tick(R(600))  # 10-min hole > gap_event_s
    d.process_tick(R(610))
    kinds = _chain_kinds(db)
    assert "gap" in kinds


def test_door_and_power_transitions_chained(daemon):
    d, store, db = daemon
    d.process_tick(R(0, door=False, power=True))
    d.process_tick(R(10, door=True, power=True))
    d.process_tick(R(20, door=True, power=False))
    kinds = _chain_kinds(db)
    assert "door" in kinds and "power" in kinds


def test_reflex_fires_buzzer_and_chains(tmp_path):
    csv = tiny_door_csv(tmp_path / "door.csv")
    store = EdgeStore(tmp_path / "d.db")
    d = EdgeDaemon(store)
    src = CsvSource(csv)
    while (r := src.read()) is not None:
        d.process_tick(r)
    assert len(d.buzzer.activations) >= 1  # door_timer -> sound_alarm
    assert "reflex" in _chain_kinds(tmp_path / "d.db")
    assert store.pending_count() >= 1  # escalation queued (no diagnoser wired)
    store.close()


def test_heartbeat_emitted_after_interval(tmp_path):
    store = EdgeStore(tmp_path / "d.db")
    cfg = DaemonConfig(heartbeat_s=100.0)
    d = EdgeDaemon(store, config=cfg)
    d.process_tick(R(0))
    r = d.process_tick(R(200))  # exceeds heartbeat interval
    assert r.heartbeat is True
    assert "heartbeat" in _chain_kinds(tmp_path / "d.db")
    store.close()


# --------------------------------------------------------------------------- queue flush semantics

def test_flush_queue_without_diagnoser_returns_empty(daemon):
    d, store, db = daemon
    assert d.flush_queue(0.0) == []


def test_on_verdict_callback_fires_for_each_delivered_verdict(tmp_path):
    csv = tiny_door_csv(tmp_path / "door.csv")
    store = EdgeStore(tmp_path / "d.db")
    link = LocalAppLink(create_app(FakeQwen()), online=True)
    diag = DiagnoserClient(link, dev_keys().sealing_public)
    seen: list[dict] = []
    d = EdgeDaemon(store, diagnoser=diag, on_verdict=seen.append)
    src = CsvSource(csv)
    while (r := src.read()) is not None:
        d.process_tick(r)
    assert seen and seen[0]["verdict"]["cause"] == "door_ajar"
    assert seen == d.verdicts  # the callback saw exactly what got recorded
    store.close()


def test_update_edge_rules_verdict_action_logs_annotation_not_a_bundle_swap(tmp_path):
    """update_edge_rules can only ever arrive as a *signed* bundle via /distill —
    a verdict action naming it is logged as a proposal, never auto-applied."""

    class _UpdateRulesQwen(FakeQwen):
        def chat(self, model, messages, **kw):  # type: ignore[override]
            result = super().chat(model, messages, **kw)
            if model != MODEL_DIAGNOSIS:
                return result
            body = json.loads(result.content)
            body["actions"].append({"tool": "update_edge_rules"})
            return ChatResult(
                content=json.dumps(body), task_id=result.task_id, model=result.model,
                prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                thinking=result.thinking,
            )

    csv = tiny_door_csv(tmp_path / "door.csv")
    db = tmp_path / "d.db"
    store = EdgeStore(db)
    link = LocalAppLink(create_app(_UpdateRulesQwen()), online=True)
    diag = DiagnoserClient(link, dev_keys().sealing_public)
    d = EdgeDaemon(store, diagnoser=diag)
    src = CsvSource(csv)
    while (r := src.read()) is not None:
        d.process_tick(r)
    entries = [json.loads(e) for (e,) in sql(db, "SELECT entry FROM log_chain ORDER BY seq")]
    proposals = [
        e for e in entries
        if e["kind"] == "annotation" and e["payload"].get("note") == "rules update proposed"
    ]
    assert proposals, "update_edge_rules verdict action should be logged as a proposal"
    assert d.rules_version == 1  # never actually swapped — only the signed /distill path can do that
    assert d.dispatcher.unknown == []  # not routed as an unknown tool either
    store.close()


# --------------------------------------------------------------------------- I3 gate

def test_activate_valid_signed_bundle_hotswaps(daemon):
    d, store, db = daemon
    b = _v2_bundle()
    sig = sign_bundle(b, dev_keys().signing_seed)
    assert d.try_activate_bundle(b, sig, T0) is True
    assert d.rules_version == 2 and store.active_rules()[0] == 2
    assert "rules_activated" in _chain_kinds(db)


def test_I3_refuses_unsigned_bundle_and_logs(daemon):
    d, store, db = daemon
    with pytest.raises(RuleBundleRejected, match="signature missing"):
        d.try_activate_bundle(_v2_bundle(), None, T0)
    assert d.rules_version == 1  # unchanged
    assert "rule_bundle_rejected" in _chain_kinds(db)


def test_I3_refuses_invalid_signature(daemon):
    d, store, db = daemon
    b = _v2_bundle()
    sig = sign_bundle(b, generate_signing_seed())  # wrong signer
    with pytest.raises(RuleBundleRejected, match="signature invalid"):
        d.try_activate_bundle(b, sig, T0)
    assert d.rules_version == 1


def test_I3_refuses_downgrade(daemon):
    d, store, db = daemon
    # a correctly-signed v1 must still be refused because active is already v1
    b = builtin_bundle_dict()
    sig = sign_bundle(b, dev_keys().signing_seed)
    with pytest.raises(RuleBundleRejected, match="downgrade"):
        d.try_activate_bundle(b, sig, T0)


def test_I3_refuses_schema_invalid(daemon):
    d, store, db = daemon
    with pytest.raises(RuleBundleRejected, match="schema invalid"):
        d.try_activate_bundle({"schema": "permafrost.rules/v1", "version": 2, "rules": []}, "deadbeef", T0)


def test_I3_signature_checked_before_version(tmp_path):
    # order matters: an unsigned DOWNGRADE reports the signature failure first
    store = EdgeStore(tmp_path / "d.db")
    chain_and_daemon = EdgeDaemon(store)
    with pytest.raises(RuleBundleRejected, match="signature"):
        chain_and_daemon.try_activate_bundle(builtin_bundle_dict(), None, T0)
    store.close()


def test_activate_bundle_on_store_standalone(tmp_path):
    from permafrost.chain import ChainLogger
    store = EdgeStore(tmp_path / "d.db")
    chain = ChainLogger(store)
    b = _v2_bundle()
    sig = sign_bundle(b, dev_keys().signing_seed)
    parsed = activate_bundle_on_store(store, chain, b, sig, dev_keys().verify_key, T0)
    assert parsed.version == 2
    store.close()
