"""ExcursionVerdict — the structured contract between brain and siren (SPEC §6).

The schema *is* the safety interface: verdicts drive actuators, so a malformed
verdict must fail loudly at parse time, and a CRITICAL verdict without a
guidance citation is invalid by construction (invariant I4).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["CAUSES", "ACTION_TOOLS", "RiskInfo", "VerdictAction", "ExcursionVerdict", "is_critical_dict"]

CAUSES = (
    "door_ajar",
    "defrost_cycle",
    "compressor_degradation",
    "power_loss",
    "ambient_heat",
    "unknown",
)

ACTION_TOOLS = ("sound_alarm", "notify", "annotate_log", "schedule_service", "update_edge_rules")

# A verdict is CRITICAL when it is not benign AND it either demands the siren
# or puts stock at risk within the hour.
_CRITICAL_ETA_MIN = 60


class RiskInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stock_at_risk_in_min: int | None = Field(default=None, ge=0)
    vfc_grade_impact: str | None = None


class VerdictAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["sound_alarm", "notify", "annotate_log", "schedule_service", "update_edge_rules"]
    now: bool | None = None
    channel: str | None = None
    note: str | None = None


class ExcursionVerdict(BaseModel):
    """SPEC §6 shape: cause/confidence/benign/risk/evidence/guidance_citation/actions."""

    model_config = ConfigDict(extra="forbid")

    cause: Literal[
        "door_ajar", "defrost_cycle", "compressor_degradation", "power_loss", "ambient_heat", "unknown"
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    benign: bool
    risk: RiskInfo = Field(default_factory=RiskInfo)
    evidence: list[str] = Field(min_length=1)
    guidance_citation: str = ""
    actions: list[VerdictAction] = Field(default_factory=list)

    @property
    def is_critical(self) -> bool:
        return _critical(self.benign, [a.tool for a in self.actions], self.risk.stock_at_risk_in_min)

    @model_validator(mode="after")
    def _critical_needs_citation(self) -> "ExcursionVerdict":
        # Invariant I4: every CRITICAL verdict carries >=1 guidance citation.
        if self.is_critical and not self.guidance_citation.strip():
            raise ValueError("CRITICAL verdict must carry >=1 guidance citation (invariant I4)")
        return self


def _critical(benign: bool, tools: list[str], eta_min: int | None) -> bool:
    if benign:
        return False
    if "sound_alarm" in tools:
        return True
    return eta_min is not None and eta_min <= _CRITICAL_ETA_MIN


def is_critical_dict(verdict: dict[str, Any]) -> bool:
    """Criticality check for a raw verdict dict (pre-validation paths)."""
    tools = [a.get("tool", "") for a in verdict.get("actions", [])]
    eta = (verdict.get("risk") or {}).get("stock_at_risk_in_min")
    return _critical(bool(verdict.get("benign", False)), tools, eta)
