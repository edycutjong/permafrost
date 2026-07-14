"""ExcursionVerdict — the structured brain->siren contract, incl. invariant I4."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from permafrost.verdict import (
    ACTION_TOOLS,
    CAUSES,
    ExcursionVerdict,
    RiskInfo,
    VerdictAction,
    is_critical_dict,
)


def _v(**over):
    base = {
        "cause": "defrost_cycle",
        "confidence": 0.9,
        "benign": True,
        "risk": {"stock_at_risk_in_min": None, "vfc_grade_impact": None},
        "evidence": ["bounded sawtooth spike"],
        "guidance_citation": "",
        "actions": [{"tool": "annotate_log"}],
    }
    base.update(over)
    return base


def test_benign_verdict_parses_without_citation():
    v = ExcursionVerdict.model_validate(_v())
    assert v.benign and not v.is_critical


def test_all_causes_accepted():
    for cause in CAUSES:
        benign = cause == "defrost_cycle"
        v = _v(cause=cause, benign=benign, guidance_citation="CDC ref", actions=[{"tool": "notify", "channel": "phone"}])
        assert ExcursionVerdict.model_validate(v).cause == cause


def test_critical_alarm_without_citation_raises_I4():
    bad = _v(benign=False, guidance_citation="", actions=[{"tool": "sound_alarm", "now": True}])
    with pytest.raises(ValidationError, match="I4"):
        ExcursionVerdict.model_validate(bad)


def test_critical_alarm_with_citation_ok():
    ok = _v(
        cause="door_ajar",
        benign=False,
        guidance_citation="CDC Toolkit: excursion >15min requires assessment",
        actions=[{"tool": "sound_alarm", "now": True}],
    )
    v = ExcursionVerdict.model_validate(ok)
    assert v.is_critical and v.guidance_citation


def test_whitespace_citation_is_not_a_citation():
    bad = _v(benign=False, guidance_citation="   ", actions=[{"tool": "sound_alarm", "now": True}])
    with pytest.raises(ValidationError, match="I4"):
        ExcursionVerdict.model_validate(bad)


def test_eta_within_hour_is_critical_and_needs_citation():
    bad = _v(
        cause="power_loss",
        benign=False,
        risk={"stock_at_risk_in_min": 30, "vfc_grade_impact": "B->C"},
        guidance_citation="",
        actions=[{"tool": "notify", "channel": "phone"}],
    )
    with pytest.raises(ValidationError, match="I4"):
        ExcursionVerdict.model_validate(bad)


def test_eta_beyond_hour_not_critical_no_citation_needed():
    v = _v(
        cause="compressor_degradation",
        benign=False,
        risk={"stock_at_risk_in_min": 120, "vfc_grade_impact": "A->B"},
        guidance_citation="",
        actions=[{"tool": "schedule_service"}],
    )
    parsed = ExcursionVerdict.model_validate(v)
    assert not parsed.is_critical


def test_benign_is_never_critical_even_with_alarm():
    # benign short-circuits criticality
    v = ExcursionVerdict.model_validate(_v(benign=True, guidance_citation="x", actions=[{"tool": "annotate_log"}]))
    assert not v.is_critical


def test_extra_field_forbidden():
    with pytest.raises(ValidationError):
        ExcursionVerdict.model_validate(_v(surprise="nope"))


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        ExcursionVerdict.model_validate(_v(confidence=1.5))
    with pytest.raises(ValidationError):
        ExcursionVerdict.model_validate(_v(confidence=-0.1))


def test_evidence_must_be_nonempty():
    with pytest.raises(ValidationError):
        ExcursionVerdict.model_validate(_v(evidence=[]))


def test_unknown_action_tool_rejected():
    with pytest.raises(ValidationError):
        ExcursionVerdict.model_validate(_v(actions=[{"tool": "launch_missile"}]))


def test_negative_eta_rejected():
    with pytest.raises(ValidationError):
        RiskInfo(stock_at_risk_in_min=-5)


def test_risk_extra_forbidden():
    with pytest.raises(ValidationError):
        RiskInfo.model_validate({"stock_at_risk_in_min": 5, "junk": 1})


def test_action_tool_literal_set_matches_constant():
    assert set(ACTION_TOOLS) == {"sound_alarm", "notify", "annotate_log", "schedule_service", "update_edge_rules"}
    for tool in ACTION_TOOLS:
        assert VerdictAction(tool=tool).tool == tool


def test_is_critical_dict_matches_property():
    alarm = _v(benign=False, guidance_citation="c", actions=[{"tool": "sound_alarm", "now": True}])
    assert is_critical_dict(alarm) is True
    benign = _v()
    assert is_critical_dict(benign) is False
    eta = {"benign": False, "risk": {"stock_at_risk_in_min": 10}, "actions": []}
    assert is_critical_dict(eta) is True
