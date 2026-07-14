"""Cloud /distill: verdict history -> compact Ed25519-signed rule bundle (the self-teaching loop)."""

from __future__ import annotations

import pytest

from permafrost.chain import ChainLogger
from permafrost.cloud.distill import DistillRequest, distill
from permafrost.crypto import dev_keys, verify_key_of
from permafrost.daemon import activate_bundle_on_store
from permafrost.qwen.fake import FakeQwen
from permafrost.qwen.transport import MODEL_DISTILL, ChatResult, UsageMeter
from permafrost.rules import RuleBundle, RuleBundleRejected, sign_bundle
from permafrost.storage import EdgeStore
from permafrost.timeutil import VIRTUAL_EPOCH_TS as T0


def _distill(causes, current_version=1):
    verdicts = [{"cause": c, "benign": c == "defrost_cycle"} for c in causes]
    return distill(DistillRequest(verdicts=verdicts, current_version=current_version, now_ts=T0), FakeQwen(), dev_keys().signing_seed)


class _StubDistillTransport:
    """Returns fixed ``chat`` content — forces distill()'s two error paths (non-JSON
    bundle, schema-invalid bundle) that FakeQwen's real distiller never produces."""

    def __init__(self, content: str):
        self.usage = UsageMeter()
        self._content = content

    def chat(self, model, messages, **kw):
        return ChatResult(content=self._content, task_id="stub", model=model, prompt_tokens=1, completion_tokens=1)


def test_distill_produces_valid_signed_bundle():
    res = _distill(["defrost_cycle"] * 3)
    bundle = RuleBundle.parse(res.bundle)
    assert bundle.verify(res.sig, res.verify_key)
    assert res.model == MODEL_DISTILL and res.task_id.startswith("fake-")


def test_distill_verify_key_matches_dev_signing_seed():
    res = _distill(["defrost_cycle"])
    assert res.verify_key == verify_key_of(dev_keys().signing_seed) == dev_keys().verify_key


def test_distilled_version_exceeds_current():
    res = _distill(["defrost_cycle"], current_version=7)
    assert res.bundle["version"] == 8


def test_defrost_history_adds_recognizer():
    res = _distill(["defrost_cycle"] * 4)
    ids = {r["id"] for r in res.bundle["rules"]}
    assert "defrost_recognizer" in ids
    recog = next(r for r in res.bundle["rules"] if r["id"] == "defrost_recognizer")
    assert recog["type"] == "pattern_defrost" and recog["escalate"] is False
    assert "fast_rise" in recog["suppresses"]


def test_door_history_tightens_door_timer():
    res = _distill(["door_ajar"] * 2)
    door = next(r for r in res.bundle["rules"] if r["id"] == "door_timer")
    assert door["max_open_s"] == 90  # tightened from builtin 120


def test_compressor_history_tightens_drift_rule():
    res = _distill(["compressor_degradation"] * 2)
    drift = next(r for r in res.bundle["rules"] if r["id"] == "slow_drift")
    assert drift["per_day"] == 0.2


def test_distilled_bundle_is_new_source():
    res = _distill(["defrost_cycle"])
    assert res.bundle["source"] == "distilled"


def test_distill_if_then_text_present():
    res = _distill(["defrost_cycle"])
    assert res.if_then.startswith("# rules v2")


def test_distill_raises_on_non_json_bundle():
    transport = _StubDistillTransport("not json at all")
    with pytest.raises(ValueError, match="non-JSON bundle"):
        distill(DistillRequest(verdicts=[], current_version=1, now_ts=T0), transport, dev_keys().signing_seed)


def test_distill_raises_on_schema_invalid_bundle():
    import json
    transport = _StubDistillTransport(json.dumps({"not": "a rule bundle at all"}))
    with pytest.raises(ValueError, match="schema validation"):
        distill(DistillRequest(verdicts=[], current_version=1, now_ts=T0), transport, dev_keys().signing_seed)


def test_distill_rejects_non_advancing_version():
    # a hostile transport could return version <= current; distill must reject
    class LowballQwen(FakeQwen):
        def chat(self, model, messages, **kw):  # type: ignore[override]
            import json
            from permafrost.rules import builtin_bundle_dict
            from permafrost.qwen.transport import ChatResult
            b = builtin_bundle_dict()  # version 1, <= current
            return ChatResult(content=json.dumps(b), task_id="fake-x", model=model, prompt_tokens=1, completion_tokens=1)

    with pytest.raises(ValueError, match="must exceed"):
        distill(DistillRequest(verdicts=[{"cause": "defrost_cycle"}], current_version=1, now_ts=T0), LowballQwen(), dev_keys().signing_seed)


# --------------------------------------------------------------------------- I3: the edge refuses an UNSIGNED distilled bundle

def test_edge_refuses_unsigned_distilled_bundle(tmp_path):
    res = _distill(["defrost_cycle"] * 3)
    store = EdgeStore(tmp_path / "e.db")
    chain = ChainLogger(store)
    try:
        with pytest.raises(RuleBundleRejected, match="signature missing"):
            activate_bundle_on_store(store, chain, res.bundle, None, dev_keys().verify_key, T0)
    finally:
        store.close()


def test_edge_accepts_properly_signed_distilled_bundle(tmp_path):
    res = _distill(["defrost_cycle"] * 3)
    store = EdgeStore(tmp_path / "e.db")
    chain = ChainLogger(store)
    try:
        parsed = activate_bundle_on_store(store, chain, res.bundle, res.sig, dev_keys().verify_key, T0)
        assert parsed.version == 2
        assert store.active_rules()[0] == 2
    finally:
        store.close()


def test_resigning_bundle_with_wrong_key_is_refused(tmp_path):
    res = _distill(["defrost_cycle"])
    from permafrost.crypto import generate_signing_seed
    wrong_sig = sign_bundle(res.bundle, generate_signing_seed())
    store = EdgeStore(tmp_path / "e.db")
    chain = ChainLogger(store)
    try:
        with pytest.raises(RuleBundleRejected, match="signature invalid"):
            activate_bundle_on_store(store, chain, res.bundle, wrong_sig, dev_keys().verify_key, T0)
    finally:
        store.close()
