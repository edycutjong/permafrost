"""Weekly compliance report rendering + edge-db mining (reporting.py)."""

from __future__ import annotations

from permafrost.cloud.report import weekly_report_markdown
from permafrost.reporting import chain_entries, edge_weekly_report, verdict_history


def _verdicts():
    # no "ts" key -> the renderer includes them in any week (matches real callers,
    # which always pass a real float ts or omit it entirely)
    return [
        {"cause": "door_ajar", "benign": False, "confidence": 0.93,
         "risk": {"stock_at_risk_in_min": 22}, "guidance_citation": "CDC: door ajar",
         "actions": [{"tool": "sound_alarm", "now": True}], "task_id": "fake-abc"},
        {"cause": "defrost_cycle", "benign": True, "confidence": 0.95,
         "risk": {}, "guidance_citation": "", "actions": [{"tool": "annotate_log"}], "task_id": "fake-def"},
    ]


def test_report_has_title_and_summary():
    md = weekly_report_markdown(2, _verdicts())
    assert "# Permafrost weekly compliance report — ISO week 2" in md
    assert "Excursion verdicts: **2**" in md and "1 benign, 1 critical" in md


def test_report_lists_causes_table():
    md = weekly_report_markdown(2, _verdicts())
    assert "| cause | count |" in md and "| door_ajar | 1 |" in md


def test_report_critical_section_cites_guidance():
    md = weekly_report_markdown(2, _verdicts())
    assert "Critical events" in md and "CDC: door ajar" in md and "invariant I4" in md


def test_report_lists_task_ids():
    md = weekly_report_markdown(2, _verdicts())
    assert "fake-abc" in md


def test_report_empty_week_is_frozen_safe():
    md = weekly_report_markdown(5, [])
    assert "frozen safe" in md


def test_report_optional_metrics_render():
    md = weekly_report_markdown(2, _verdicts(), readings=8000, in_band_pct=99.4, roots_signed=3, chain_summary="OK")
    assert "Readings logged: **8000**" in md
    assert "Time in 2-8 C band: **99.4%**" in md
    assert "Signed daily Merkle roots: **3**" in md


def test_report_mentions_batch_pricing():
    md = weekly_report_markdown(2, _verdicts())
    assert "Batch API" in md


# --------------------------------------------------------------------------- edge-db mining

def test_verdict_history_from_replayed_db(door_replay):
    result, db, _ = door_replay
    hist = verdict_history(db)
    assert hist and all("ts" in v and "task_id" in v for v in hist)
    assert any(v["cause"] == "door_ajar" for v in hist)


def test_chain_entries_iterates_in_seq_order(door_replay):
    result, db, _ = door_replay
    entries = list(chain_entries(db))
    seqs = [e["seq"] for e in entries]
    assert seqs == sorted(seqs) and seqs[0] == 1


def test_edge_weekly_report_includes_chain_audit(door_replay):
    result, db, _ = door_replay
    md = edge_weekly_report(db, 2)
    assert "Hash-chain audit" in md and "OK" in md
    assert "Readings logged" in md


def test_edge_weekly_report_skips_entries_outside_requested_week(door_replay):
    # every door_ajar entry lands in the virtual-clock ISO week 2 — asking for week 1
    # must filter every reading/verdict out, not just render an empty causes table.
    result, db, _ = door_replay
    md = edge_weekly_report(db, 1)
    assert "Readings logged: **0**" in md
    assert "Excursion verdicts: **0**" in md
    assert "_No excursions this week — frozen safe._" in md
