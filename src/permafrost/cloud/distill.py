"""/distill — verdict history -> compact signed IF/THEN rule bundle.

The self-teaching loop: ``qwen3.6-flash`` (cheap, repetitive codegen tier)
compiles what the expensive brain has learned about THIS fridge into local
reflex rules, and the cloud Ed25519-signs the bundle. The edge refuses
anything unsigned (invariant I3), so the loop is cryptographically gated.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..crypto import verify_key_of
from ..qwen.transport import MODEL_DISTILL, QwenTransport
from ..rules import RuleBundle, RuleBundleInvalid, sign_bundle

__all__ = ["DistillRequest", "DistillResult", "distill"]

_SYSTEM_PROMPT = (
    "You are Permafrost's rule distiller. Given a summary of cloud diagnosis "
    "verdicts for one fridge, compile a compact local reflex rule bundle "
    "(schema permafrost.rules/v1) that resolves the fridge's PROVEN-benign "
    "patterns locally and tightens rules for its proven risks. Reply ONLY with "
    "the bundle JSON."
)


class DistillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdicts: list[dict[str, Any]] = Field(default_factory=list)
    current_version: int = 1
    now_ts: float = 0.0


class DistillResult(BaseModel):
    bundle: dict[str, Any]
    sig: str
    verify_key: str
    task_id: str
    model: str
    if_then: str


def _history_summary(req: DistillRequest) -> dict[str, Any]:
    counts = Counter(str(v.get("cause", "unknown")) for v in req.verdicts)
    benign = sum(1 for v in req.verdicts if v.get("benign"))
    return {
        "task": "distill",
        "current_version": req.current_version,
        "now_ts": req.now_ts,
        "cause_counts": dict(sorted(counts.items())),
        "total_verdicts": len(req.verdicts),
        "benign_verdicts": benign,
    }


def distill(req: DistillRequest, transport: QwenTransport, signing_seed_hex: str) -> DistillResult:
    summary = _history_summary(req)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Verdict history summary:\n```json\n" + json.dumps(summary, sort_keys=True) + "\n```",
        },
    ]
    result = transport.chat(MODEL_DISTILL, messages, thinking=False, json_response=True)

    try:
        bundle_dict = json.loads(result.content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"distiller returned non-JSON bundle: {exc}") from exc

    try:
        bundle = RuleBundle.parse(bundle_dict)  # schema gate BEFORE signing
    except RuleBundleInvalid as exc:
        raise ValueError(f"distilled bundle failed schema validation: {exc}") from exc

    if bundle.version <= req.current_version:
        raise ValueError(
            f"distilled bundle version {bundle.version} must exceed current {req.current_version}"
        )

    sig = sign_bundle(bundle.raw, signing_seed_hex)
    return DistillResult(
        bundle=bundle.raw,
        sig=sig,
        verify_key=verify_key_of(signing_seed_hex),
        task_id=result.task_id,
        model=result.model,
        if_then=bundle.if_then_text(),
    )
