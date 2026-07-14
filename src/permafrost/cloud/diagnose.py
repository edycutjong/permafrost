"""/diagnose — curve + metadata -> ExcursionVerdict (the cloud brain).

Pipeline (identical for LiveQwen and FakeQwen):

1. compute deterministic CurveFeatures from the submitted curve + daily means
2. retrieve guidance snippets (text-embedding-v4 through the transport)
3. build the prompt: engineer persona + features + curve + fridge metadata +
   guidance, with a machine-readable ```json payload block
4. ``qwen3.7-plus`` with the thinking flag, JSON response, action tools
5. parse -> backfill citation from retrieval if the model left it empty
   (invariant I4 belt-and-braces; the schema validator is the final gate)
6. validate as ExcursionVerdict — a malformed verdict fails HERE, never at
   the siren.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..features import CurvePoint, extract_features
from ..qwen.transport import MODEL_DIAGNOSIS, QwenTransport
from ..verdict import ExcursionVerdict
from .guidance import GuidanceStore, format_citation
from .tools import ACTION_TOOL_DEFS

__all__ = ["DiagnoseRequest", "DiagnoseResult", "diagnose"]

_SYSTEM_PROMPT = (
    "You are Permafrost's refrigeration diagnosis engine: a careful refrigeration "
    "engineer for clinic vaccine fridges (2-8 C band). You receive a downsampled "
    "temperature/humidity/door/power curve, computed features, fridge metadata and "
    "retrieved cold-chain guidance. Reason step by step about slopes, periodicity, "
    "humidity co-signals and gaps; distinguish benign defrost cycles from real "
    "excursions; never cry wolf. Reply ONLY with an ExcursionVerdict JSON object "
    "with keys: cause (door_ajar|defrost_cycle|compressor_degradation|power_loss|"
    "ambient_heat|unknown), confidence (0-1), benign (bool), risk "
    "{stock_at_risk_in_min, vfc_grade_impact}, evidence (list of strings), "
    "guidance_citation (quote one retrieved snippet), actions (list of "
    "{tool, now?, channel?, note?} drawn from the provided tools)."
)


class DiagnoseRequest(BaseModel):
    """The edge->cloud event contract (SPEC §5): events, never raw streams."""

    model_config = ConfigDict(extra="forbid")

    fridge_meta: dict[str, Any] = Field(default_factory=dict)
    curve: list[dict[str, Any]] = Field(min_length=2)
    door_events: list[dict[str, Any]] = Field(default_factory=list)
    power_events: list[dict[str, Any]] = Field(default_factory=list)
    daily_means: list[list[Any]] = Field(default_factory=list)
    trigger: dict[str, Any] = Field(default_factory=dict)


class DiagnoseResult(BaseModel):
    verdict: dict[str, Any]
    task_id: str
    model: str
    thinking: bool
    guidance_ids: list[str]
    prompt_tokens: int
    completion_tokens: int


def _build_messages(
    req: DiagnoseRequest, features: dict[str, Any], guidance_texts: list[str]
) -> list[dict[str, str]]:
    payload = {
        "task": "diagnose",
        "features": features,
        "fridge_meta": req.fridge_meta,
        "trigger": req.trigger,
        "door_events": req.door_events[-20:],
        "power_events": req.power_events[-20:],
        "curve_points": len(req.curve),
        "curve_head": req.curve[:3],
        "curve_tail": req.curve[-3:],
    }
    user = (
        "Diagnose this excursion.\n\n"
        "Retrieved guidance (cite at least one):\n- " + "\n- ".join(guidance_texts) + "\n\n"
        "Machine-readable event payload:\n"
        "```json\n" + json.dumps(payload, sort_keys=True) + "\n```\n\n"
        "Full downsampled curve (ts, temp_c, humidity_pct, door_open, power_ok):\n"
        + json.dumps(req.curve[:360])
    )
    return [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}]


def diagnose(req: DiagnoseRequest, transport: QwenTransport, store: GuidanceStore) -> DiagnoseResult:
    points = [CurvePoint.from_dict(p) for p in req.curve]
    daily_means = [(str(d), float(m)) for d, m in req.daily_means]
    features = extract_features(points, daily_means).to_dict()

    # first-pass retrieval keyed on the deterministic feature classification —
    # the live model sees relevant authority in-context.
    from ..qwen.fake import classify_features

    probable = classify_features(features)
    hits = store.retrieve(store.query_for(probable, features), k=2)
    citations = [format_citation(s) for s, _ in hits]
    guidance_ids = [s.id for s, _ in hits]

    messages = _build_messages(req, features, citations)
    result = transport.chat(
        MODEL_DIAGNOSIS, messages, thinking=True, json_response=True, tools=ACTION_TOOL_DEFS
    )

    try:
        raw = json.loads(result.content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"diagnosis model returned non-JSON verdict: {exc}") from exc

    if not str(raw.get("guidance_citation", "")).strip():
        raw["guidance_citation"] = citations[0] if citations else ""

    try:
        verdict = ExcursionVerdict.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"diagnosis verdict failed schema validation: {exc}") from exc

    return DiagnoseResult(
        verdict=verdict.model_dump(),
        task_id=result.task_id,
        model=result.model,
        thinking=result.thinking,
        guidance_ids=guidance_ids,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
