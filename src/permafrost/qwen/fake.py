"""FakeQwen — deterministic, offline, keyless stand-in for Qwen Cloud.

- ``chat`` (diagnosis): reads the machine-readable payload block the prompt
  builder embeds, classifies the curve from the SAME features a live model is
  shown, and returns a fixture ExcursionVerdict for that curve class.
- ``chat`` (distillation): compiles a deterministic rule bundle from the
  verdict-history summary in the payload.
- ``embed``: hashing-trick bag-of-words vectors (dim 256) — deterministic,
  cosine tracks token overlap, so guidance retrieval stays *relevant* offline.
- ``tts``: deterministic pseudo-audio bytes.

Every output is a pure function of the input, which is what lets bench.py
assert a >=0.9 confusion-matrix floor and the replay tests assert
byte-identical chains across runs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from .transport import (
    ALLOWED_MODELS,
    MODEL_DIAGNOSIS,
    MODEL_DISTILL,
    ChatResult,
    UsageMeter,
)

__all__ = ["FakeQwen", "classify_features", "fixture_verdict", "hash_embed"]

_PAYLOAD_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
EMBED_DIM = 256


# --------------------------------------------------------------------------- classifier


def classify_features(f: dict[str, Any]) -> str:
    """Deterministic cause classification from CurveFeatures (see features.py).

    Mirrors the discriminators engineered into the seed curves; order matters:
    power evidence first, then multi-day drift, then the door-vs-defrost twin
    split on humidity + door signals.
    """
    true_gap = f.get("gap_max_s", 0) > 900 and f.get("gap_ratio", 0.0) > 5.0
    if true_gap or f.get("power_out_seen", False):
        return "power_loss"
    if f.get("drift_days", 0) >= 3 and f.get("drift_c_per_day", 0.0) >= 0.2:
        return "compressor_degradation"
    if f.get("door_open_any", False) and f.get("humidity_delta", 0.0) >= 10.0:
        return "door_ajar"
    if f.get("spike_episodes", 0) >= 1 and f.get("humidity_delta", 0.0) < 10.0:
        return "defrost_cycle"
    if f.get("peak_delta_c", 0.0) >= 1.5:
        return "ambient_heat"
    return "unknown"


def fixture_verdict(cause: str, features: dict[str, Any]) -> dict[str, Any]:
    """Fixture ExcursionVerdict per curve class (guidance_citation filled by the pipeline)."""
    peak = features.get("peak_delta_c", 0.0)
    rise = features.get("max_rise_c_per_min", 0.0)
    if cause == "door_ajar":
        return {
            "cause": "door_ajar",
            "confidence": 0.93,
            "benign": False,
            "risk": {"stock_at_risk_in_min": 22, "vfc_grade_impact": "B->D"},
            "evidence": [
                f"monotonic rise +{rise:.2f}C/min with door_open=true",
                f"humidity spike (+{features.get('humidity_delta', 0.0):.1f} points) — ambient air ingress",
                "no 6h periodicity: not a defrost signature",
            ],
            "guidance_citation": "",
            "actions": [
                {"tool": "sound_alarm", "now": True},
                {"tool": "notify", "channel": "phone"},
                {"tool": "annotate_log"},
            ],
        }
    if cause == "defrost_cycle":
        return {
            "cause": "defrost_cycle",
            "confidence": 0.95,
            "benign": True,
            "risk": {"stock_at_risk_in_min": None, "vfc_grade_impact": None},
            "evidence": [
                f"bounded sawtooth spike (+{peak:.1f}C) matching the fridge's auto-defrost spec",
                "humidity flat throughout the spike — no door/ambient air ingress",
                "recurs on a ~6h period; cabinet returns to setpoint unaided",
            ],
            "guidance_citation": "",
            "actions": [{"tool": "annotate_log", "note": "defrost cycle — benign, no action"}],
        }
    if cause == "compressor_degradation":
        return {
            "cause": "compressor_degradation",
            "confidence": 0.87,
            "benign": False,
            "risk": {"stock_at_risk_in_min": None, "vfc_grade_impact": "A->B"},
            "evidence": [
                f"daily mean drifting +{features.get('drift_c_per_day', 0.0):.2f}C/day over "
                f"{features.get('drift_days', 0)} days",
                "defrost pattern intact — controller fine, cooling capacity falling",
                "curve-only inference: service flag, not a certainty (no refrigerant telemetry)",
            ],
            "guidance_citation": "",
            "actions": [
                {"tool": "schedule_service", "note": "compressor/gasket inspection within 2 weeks"},
                {"tool": "notify", "channel": "email"},
            ],
        }
    if cause == "power_loss":
        return {
            "cause": "power_loss",
            "confidence": 0.90,
            "benign": False,
            "risk": {"stock_at_risk_in_min": 45, "vfc_grade_impact": "B->C"},
            "evidence": [
                f"telemetry gap of {features.get('gap_max_s', 0.0) / 60.0:.0f} min (device blackout)",
                "recovery curve: elevated cabinet temp decaying to setpoint after restore",
                "mains-out flag seen before the gap",
            ],
            "guidance_citation": "",
            "actions": [
                {"tool": "notify", "channel": "phone"},
                {"tool": "annotate_log", "note": "power excursion — verify stock per guidance"},
            ],
        }
    if cause == "ambient_heat":
        return {
            "cause": "ambient_heat",
            "confidence": 0.60,
            "benign": False,
            "risk": {"stock_at_risk_in_min": None, "vfc_grade_impact": None},
            "evidence": ["sustained elevation without door/humidity/periodicity signature"],
            "guidance_citation": "",
            "actions": [{"tool": "notify", "channel": "phone"}],
        }
    return {
        "cause": "unknown",
        "confidence": 0.40,
        "benign": False,
        "risk": {"stock_at_risk_in_min": None, "vfc_grade_impact": None},
        "evidence": ["curve does not match any known signature"],
        "guidance_citation": "",
        "actions": [{"tool": "notify", "channel": "phone"}],
    }


# --------------------------------------------------------------------------- embeddings


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def hash_embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Hashing-trick embedding: deterministic, offline, cosine ~ token overlap."""
    vec = [0.0] * dim
    for tok in _tokens(text):
        digest = hashlib.sha256(tok.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


# --------------------------------------------------------------------------- distiller


def _distill_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic rule compilation from a verdict-history summary.

    IF the history proves the fridge has a benign defrost signature THEN ship a
    recognizer that resolves it locally (no cloud escalation) — the measured
    cost saving in bench.py. Door/compressor history tightens those rules.
    """
    from ..rules import builtin_bundle_dict  # local import avoids a cycle

    counts: dict[str, int] = payload.get("cause_counts", {})
    current_version = int(payload.get("current_version", 1))
    bundle = builtin_bundle_dict()
    bundle["version"] = current_version + 1
    bundle["source"] = "distilled"
    bundle["created_ts"] = float(payload.get("now_ts", 0.0))

    rules = {r["id"]: r for r in bundle["rules"]}
    if counts.get("defrost_cycle", 0) >= 1:
        bundle["rules"].append(
            {
                "id": "defrost_recognizer",
                "type": "pattern_defrost",
                "max_peak_delta_c": 3.6,
                "max_duration_s": 1500,
                "max_humidity_delta": 6.0,
                "severity": "info",
                "actions": ["annotate_log"],
                "escalate": False,
                "suppresses": ["fast_rise", "band_high"],
                "message": "Defrost signature recognized locally — benign, logged, not escalated",
            }
        )
    if counts.get("door_ajar", 0) >= 1:
        rules["door_timer"]["max_open_s"] = 90
        rules["door_timer"]["message"] = "Door open 90s+ (tightened: door history on this fridge)"
    if counts.get("compressor_degradation", 0) >= 1:
        rules["slow_drift"]["per_day"] = 0.2
    return bundle


# --------------------------------------------------------------------------- transport


class FakeQwen:
    """Deterministic QwenTransport. Same protocol as LiveQwen, zero network."""

    def __init__(self) -> None:
        self.usage = UsageMeter()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _payload_from(messages: list[dict[str, str]]) -> dict[str, Any]:
        text = "\n".join(m.get("content", "") for m in messages)
        blocks = _PAYLOAD_RE.findall(text)
        if not blocks:
            return {}
        try:
            return json.loads(blocks[-1])
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _task_id(payload: bytes) -> str:
        return "fake-" + hashlib.sha256(payload).hexdigest()[:12]

    # -- QwenTransport ------------------------------------------------------

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        thinking: bool = False,
        json_response: bool = True,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        if model not in ALLOWED_MODELS:
            raise ValueError(f"model {model!r} is not in Permafrost's allowed set")
        payload = self._payload_from(messages)
        if model == MODEL_DIAGNOSIS and payload.get("task") == "diagnose":
            features = payload.get("features", {})
            verdict = fixture_verdict(classify_features(features), features)
            content = json.dumps(verdict, sort_keys=True)
        elif model == MODEL_DISTILL and payload.get("task") == "distill":
            content = json.dumps(_distill_bundle(payload), sort_keys=True)
        else:
            content = json.dumps({"note": "fake-qwen: unrecognized task", "echo": payload}, sort_keys=True)
        prompt_text = "".join(m.get("content", "") for m in messages)
        p_tok, c_tok = max(1, len(prompt_text) // 4), max(1, len(content) // 4)
        self.usage.record(model, p_tok, c_tok)
        return ChatResult(
            content=content,
            task_id=self._task_id(prompt_text.encode("utf-8")),
            model=model,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            thinking=thinking,
        )

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        if model not in ALLOWED_MODELS:
            raise ValueError(f"model {model!r} is not in Permafrost's allowed set")
        self.usage.record(model, sum(max(1, len(t) // 4) for t in texts), 0)
        return [hash_embed(t) for t in texts]

    def tts(self, model: str, text: str, instructions: str) -> bytes:
        if model not in ALLOWED_MODELS:
            raise ValueError(f"model {model!r} is not in Permafrost's allowed set")
        self.usage.record(model, max(1, len(text + instructions) // 4), 0)
        return b"FAKE-TTS-PCM:" + hashlib.sha256((instructions + "|" + text).encode("utf-8")).digest()
