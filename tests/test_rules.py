"""Reflex rule engine evaluation + signed rule-bundle schema/verification."""

from __future__ import annotations

import json

import pytest

from helpers import R
from permafrost.crypto import dev_keys, generate_signing_seed, verify_key_of
from permafrost.rules import (
    RULE_TYPES,
    ReflexEngine,
    RuleBundle,
    RuleBundleInvalid,
    builtin_bundle_dict,
    sign_bundle,
)


def _bundle(rules, version=1, source="test"):
    return {
        "schema": "permafrost.rules/v1",
        "version": version,
        "source": source,
        "created_ts": 0.0,
        "rules": rules,
    }


def _engine(rules, cooldown_s=300.0):
    return ReflexEngine(RuleBundle.parse(_bundle(rules)), cooldown_s=cooldown_s)


THRESH_HI = {
    "id": "hi", "type": "threshold", "field": "temp_c", "op": ">", "value": 8.0,
    "sustain_s": 300, "severity": "critical", "actions": ["sound_alarm", "notify"],
    "escalate": True, "message": "hot",
}
THRESH_LO = {
    "id": "lo", "type": "threshold", "field": "temp_c", "op": "<", "value": 2.0,
    "sustain_s": 300, "severity": "critical", "actions": ["sound_alarm"], "escalate": True,
}
SLOPE = {"id": "rise", "type": "slope", "per_min": 0.5, "window_s": 300, "severity": "watch", "actions": ["notify"], "escalate": True}
DOOR = {"id": "door", "type": "door_timer", "max_open_s": 120, "severity": "critical", "actions": ["sound_alarm"], "escalate": True}
GAP = {"id": "gap", "type": "gap", "max_gap_s": 120, "severity": "watch", "actions": ["annotate_log"], "escalate": True}
POWER = {"id": "pwr", "type": "power", "severity": "watch", "actions": ["notify"], "escalate": True}
TREND = {"id": "drift", "type": "trend", "per_day": 0.25, "min_days": 3, "severity": "watch", "actions": ["notify"], "escalate": True}


# --------------------------------------------------------------------------- schema

def test_rule_types_constant():
    assert set(RULE_TYPES) == {"threshold", "slope", "door_timer", "gap", "power", "trend", "pattern_defrost"}


def test_builtin_bundle_parses():
    b = RuleBundle.parse(builtin_bundle_dict())
    assert b.version == 1 and b.source == "builtin" and len(b.rules) == 7


def test_builtin_signable_and_verifies():
    b = RuleBundle.parse(builtin_bundle_dict())
    seed = dev_keys().signing_seed
    sig = sign_bundle(b.raw, seed)
    assert b.verify(sig, verify_key_of(seed))


def test_parse_accepts_json_string():
    b = RuleBundle.parse(json.dumps(builtin_bundle_dict()))
    assert b.version == 1


def test_reject_non_json_string():
    with pytest.raises(RuleBundleInvalid):
        RuleBundle.parse("{not json")


def test_reject_non_object():
    with pytest.raises(RuleBundleInvalid):
        RuleBundle.parse("[1,2,3]")


def test_reject_wrong_schema():
    b = _bundle([POWER])
    b["schema"] = "other/v9"
    with pytest.raises(RuleBundleInvalid):
        RuleBundle.parse(b)


def test_reject_bad_version():
    with pytest.raises(RuleBundleInvalid):
        RuleBundle.parse(_bundle([POWER], version=0))


def test_reject_empty_rules():
    with pytest.raises(RuleBundleInvalid):
        RuleBundle.parse(_bundle([]))


def test_reject_duplicate_ids():
    with pytest.raises(RuleBundleInvalid, match="duplicate"):
        RuleBundle.parse(_bundle([POWER, dict(POWER)]))


def test_reject_rule_not_an_object():
    with pytest.raises(RuleBundleInvalid, match="must be an object"):
        RuleBundle.parse(_bundle(["not-a-dict-rule"]))


def test_reject_unknown_type():
    with pytest.raises(RuleBundleInvalid, match="unknown type"):
        RuleBundle.parse(_bundle([{**POWER, "type": "voodoo"}]))


def test_reject_bad_severity():
    with pytest.raises(RuleBundleInvalid, match="severity"):
        RuleBundle.parse(_bundle([{**POWER, "severity": "apocalyptic"}]))


def test_reject_bad_action():
    with pytest.raises(RuleBundleInvalid, match="actions"):
        RuleBundle.parse(_bundle([{**POWER, "actions": ["launch_missile"]}]))


def test_reject_non_bool_escalate():
    with pytest.raises(RuleBundleInvalid, match="escalate"):
        RuleBundle.parse(_bundle([{**POWER, "escalate": "yes"}]))


def test_reject_missing_required_field():
    incomplete = {"id": "hi", "type": "threshold", "field": "temp_c", "op": ">", "value": 8.0,
                  "severity": "critical", "actions": ["notify"], "escalate": True}  # missing sustain_s
    with pytest.raises(RuleBundleInvalid, match="sustain_s"):
        RuleBundle.parse(_bundle([incomplete]))


def test_reject_missing_id():
    with pytest.raises(RuleBundleInvalid, match="id"):
        RuleBundle.parse(_bundle([{k: v for k, v in POWER.items() if k != "id"}]))


def test_verify_rejects_missing_signature():
    b = RuleBundle.parse(builtin_bundle_dict())
    assert b.verify(None, dev_keys().verify_key) is False
    assert b.verify("", dev_keys().verify_key) is False


def test_verify_rejects_tampered_signature():
    b = RuleBundle.parse(builtin_bundle_dict())
    seed = dev_keys().signing_seed
    sig = sign_bundle(b.raw, seed)
    bad = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert b.verify(bad, verify_key_of(seed)) is False


def test_verify_rejects_wrong_key():
    b = RuleBundle.parse(builtin_bundle_dict())
    sig = sign_bundle(b.raw, dev_keys().signing_seed)
    assert b.verify(sig, verify_key_of(generate_signing_seed())) is False


def test_if_then_text_renders_every_rule():
    b = RuleBundle.parse(builtin_bundle_dict())
    txt = b.if_then_text()
    for rule in b.rules:
        assert rule["id"] in txt
    assert txt.startswith("# rules v1")


# --------------------------------------------------------------------------- evaluation

def test_threshold_high_fires_when_sustained():
    eng = _engine([THRESH_HI])
    window = [R(i * 10, temp=9.0) for i in range(31)]  # 0..300s all >8
    firings = eng.evaluate(window, R(310, temp=9.0), [], window[-1].ts)
    assert [f.rule_id for f in firings] == ["hi"]
    assert firings[0].severity == "critical" and firings[0].escalate and firings[0].actions == ["sound_alarm", "notify"]


def test_threshold_high_not_fired_without_full_window():
    eng = _engine([THRESH_HI])
    window = [R(i * 10, temp=9.0) for i in range(10)]  # only 90s of history
    assert eng.evaluate(window, R(100, temp=9.0), [], window[-1].ts) == []


def test_threshold_high_dip_breaks_sustain():
    eng = _engine([THRESH_HI])
    window = [R(i * 10, temp=9.0) for i in range(31)]
    window[20] = R(200, temp=7.0)  # a dip inside the sustain window
    assert eng.evaluate(window, R(310, temp=9.0), [], window[-1].ts) == []


def test_threshold_high_silent_when_in_band():
    eng = _engine([THRESH_HI])
    window = [R(i * 10, temp=4.0) for i in range(31)]
    assert eng.evaluate(window, R(310, temp=4.0), [], window[-1].ts) == []


def test_threshold_low_fires():
    eng = _engine([THRESH_LO])
    window = [R(i * 10, temp=1.0) for i in range(31)]
    firings = eng.evaluate(window, R(310, temp=1.0), [], window[-1].ts)
    assert [f.rule_id for f in firings] == ["lo"]


def test_slope_fires_on_fast_rise():
    eng = _engine([SLOPE])
    window = [R(i * 10, temp=4.0 + 3.0 * (i / 29)) for i in range(30)]  # 0..290, ~0.6C/min
    firings = eng.evaluate(window, R(300, temp=7.0), [], window[-1].ts)
    assert [f.rule_id for f in firings] == ["rise"]


def test_slope_silent_when_gentle():
    eng = _engine([SLOPE])
    window = [R(i * 10, temp=4.0 + 0.3 * (i / 29)) for i in range(30)]  # ~0.06C/min
    assert eng.evaluate(window, R(300, temp=4.3), [], window[-1].ts) == []


def test_door_timer_fires_after_max_open():
    eng = _engine([DOOR])
    window = [R(i * 10, door=True) for i in range(13)]  # door open 0..120
    firings = eng.evaluate(window, R(130, door=True), [], window[-1].ts)
    assert [f.rule_id for f in firings] == ["door"]


def test_door_timer_not_yet():
    eng = _engine([DOOR])
    window = [R(i * 10, door=True) for i in range(6)]  # only 50s open
    assert eng.evaluate(window, R(60, door=True), [], window[-1].ts) == []


def test_door_timer_silent_when_closed():
    eng = _engine([DOOR])
    window = [R(i * 10, door=True) for i in range(13)]
    assert eng.evaluate(window, R(130, door=False), [], window[-1].ts) == []


def test_gap_fires_on_telemetry_hole():
    eng = _engine([GAP])
    firings = eng.evaluate([R(0)], R(1000), [], prev_ts=R(800).ts)
    assert [f.rule_id for f in firings] == ["gap"]


def test_gap_silent_without_prev():
    eng = _engine([GAP])
    assert eng.evaluate([R(0)], R(1000), [], prev_ts=None) == []


def test_power_out_fires():
    eng = _engine([POWER])
    firings = eng.evaluate([R(0)], R(100, power=False), [], R(90).ts)
    assert [f.rule_id for f in firings] == ["pwr"]


def test_power_silent_when_mains_ok():
    eng = _engine([POWER])
    assert eng.evaluate([R(0)], R(100, power=True), [], R(90).ts) == []


def test_trend_fires_on_multiday_drift():
    eng = _engine([TREND])
    means = [("2026-01-05", 4.0), ("2026-01-06", 4.4), ("2026-01-07", 4.8), ("2026-01-08", 5.2)]
    firings = eng.evaluate([R(0)], R(100), means, R(90).ts)
    assert [f.rule_id for f in firings] == ["drift"]


def test_trend_silent_below_min_days():
    eng = _engine([TREND])
    means = [("2026-01-05", 4.0), ("2026-01-06", 9.0)]
    assert eng.evaluate([R(0)], R(100), means, R(90).ts) == []


def test_trend_silent_when_flat():
    eng = _engine([TREND])
    means = [("2026-01-05", 4.0), ("2026-01-06", 4.0), ("2026-01-07", 4.0), ("2026-01-08", 4.0)]
    assert eng.evaluate([R(0)], R(100), means, R(90).ts) == []


def test_trend_rearm_same_virtual_day_is_suppressed():
    """"re-fire at most daily": an episode that ends and restarts on the SAME day must
    not fire twice, even though the (cooldown-driven) episode boundary lets it re-arm."""
    eng = _engine([TREND], cooldown_s=50.0)
    means_true = [("2026-01-05", 4.0), ("2026-01-06", 4.4), ("2026-01-07", 4.8), ("2026-01-08", 5.2)]
    means_false = [("2026-01-05", 4.0), ("2026-01-06", 4.0)]  # below min_days -> cond False
    fire1 = eng.evaluate([R(0)], R(0), means_true, None)
    assert [f.rule_id for f in fire1] == ["drift"]
    # cond false for >= cooldown_s -> the episode ends (still the same virtual day)
    ended = eng.evaluate([R(0)], R(100), means_false, R(0).ts)
    assert ended == []
    # cond true again, same virtual day -> re-arms but the daily-suppression guard fires
    fire2 = eng.evaluate([R(100)], R(200), means_true, R(100).ts)
    assert fire2 == []


# --------------------------------------------------------------------------- edge trigger / debounce

def test_rule_fires_once_per_episode():
    eng = _engine([POWER])
    now = R(100, power=False)
    assert len(eng.evaluate([R(0)], now, [], R(90).ts)) == 1
    # same condition still true next tick -> no re-fire
    assert eng.evaluate([R(90, power=False)], R(110, power=False), [], now.ts) == []


def test_rule_rearms_after_cooldown():
    eng = _engine([POWER], cooldown_s=100.0)
    eng.evaluate([R(0)], R(100, power=False), [], R(90).ts)  # fire
    eng.evaluate([R(100, power=False)], R(110, power=True), [], R(100).ts)  # cond false, not yet cooled
    # after cooldown of false, the episode resets
    eng.evaluate([R(110, power=True)], R(300, power=True), [], R(110).ts)
    firings = eng.evaluate([R(300, power=True)], R(400, power=False), [], R(300).ts)
    assert [f.rule_id for f in firings] == ["pwr"]


# --------------------------------------------------------------------------- pattern_defrost suppression

DEFROST_RECOGNIZER = {
    "id": "defrost", "type": "pattern_defrost", "max_peak_delta_c": 3.6, "max_duration_s": 1500,
    "max_humidity_delta": 6.0, "severity": "info", "actions": ["annotate_log"], "escalate": False,
    "suppresses": ["rise"], "message": "defrost recognized",
}


def _defrost_window():
    window = [R(i * 10, temp=4.0) for i in range(20)]  # 0..190 baseline
    window += [R(200 + i * 10, temp=4.0 + 3.2 * (i / 30)) for i in range(30)]  # rise 200..490 (~0.53C/min)
    return window


def test_defrost_recognizer_matches_and_suppresses_slope():
    eng = _engine([DEFROST_RECOGNIZER, SLOPE])
    firings = {f.rule_id: f for f in eng.evaluate(_defrost_window(), R(500, temp=7.2), [], 490.0)}
    assert "defrost" in firings and "rise" in firings
    suppressed = firings["rise"]
    assert suppressed.suppressed_by == "defrost"
    assert suppressed.escalate is False and suppressed.severity == "info" and suppressed.actions == ["annotate_log"]


def test_slope_escalates_when_no_recognizer():
    eng = _engine([SLOPE])
    firings = {f.rule_id: f for f in eng.evaluate(_defrost_window(), R(500, temp=7.2), [], 490.0)}
    assert firings["rise"].escalate is True and firings["rise"].suppressed_by is None


def test_defrost_recognizer_does_not_match_with_door_open():
    # same spike but the door is open -> not a defrost -> slope escalates
    window = [R(i * 10, temp=4.0) for i in range(20)]
    window += [R(200 + i * 10, temp=4.0 + 3.2 * (i / 30), door=True) for i in range(30)]
    eng = _engine([DEFROST_RECOGNIZER, SLOPE])
    firings = {f.rule_id: f for f in eng.evaluate(window, R(500, temp=7.2, door=True), [], 490.0)}
    assert firings["rise"].escalate is True and firings["rise"].suppressed_by is None


def test_defrost_recognizer_does_not_match_when_peak_exceeds_limit():
    # same shape as _defrost_window() but a bigger spike (+4.5C > the 3.6C ceiling):
    # too hot to be a bounded auto-defrost -> not recognized -> slope escalates.
    window = [R(i * 10, temp=4.0) for i in range(20)]  # 0..190 baseline
    window += [R(200 + i * 10, temp=4.0 + 4.5 * (i / 30)) for i in range(30)]  # rise 200..490
    eng = _engine([DEFROST_RECOGNIZER, SLOPE])
    firings = {f.rule_id: f for f in eng.evaluate(window, R(500, temp=8.5), [], 490.0)}
    assert "defrost" not in firings
    assert firings["rise"].escalate is True and firings["rise"].suppressed_by is None


def test_defrost_recognizer_does_not_match_when_humidity_spikes():
    # bounded temp spike (matches the ceiling) but humidity co-signal betrays ambient
    # air ingress (a real door/seal problem), not a sealed-cabinet defrost cycle.
    window = [R(i * 10, temp=4.0, hum=45.0) for i in range(20)]  # 0..190 baseline, flat humidity
    window += [
        R(200 + i * 10, temp=4.0 + 3.2 * (i / 30), hum=45.0 + 40.0 * (i / 30)) for i in range(30)
    ]
    eng = _engine([DEFROST_RECOGNIZER, SLOPE])
    firings = {f.rule_id: f for f in eng.evaluate(window, R(500, temp=7.2, hum=85.0), [], 490.0)}
    assert "defrost" not in firings
    assert firings["rise"].escalate is True and firings["rise"].suppressed_by is None


def test_condition_dispatch_defaults_false_for_a_type_outside_the_known_set():
    # Defensive fallback only: the schema validator refuses any rule type not in
    # RULE_TYPES, so a real bundle can never reach this branch — exercised directly.
    eng = _engine([POWER])
    assert eng._condition({"id": "x", "type": "mystery"}, [], R(0), [], None) is False


def test_swap_bundle_clears_state():
    eng = _engine([POWER])
    eng.evaluate([R(0)], R(100, power=False), [], R(90).ts)  # fires, state remembers
    eng.swap_bundle(RuleBundle.parse(_bundle([POWER])))
    # fresh state -> fires again immediately
    firings = eng.evaluate([R(0)], R(100, power=False), [], R(90).ts)
    assert [f.rule_id for f in firings] == ["pwr"]
