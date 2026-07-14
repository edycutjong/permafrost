"""Local reflex rules: versioned, Ed25519-signed JSON bundles + the engine.

The engine is the free tier — it runs on every 10s tick with zero
connectivity and must answer in well under 100 ms (benched). Rule types:

- ``threshold``   temp above/below a value, sustained
- ``slope``       degC/min over a window
- ``door_timer``  door open longer than N seconds
- ``gap``         telemetry gap (power cut / sensor loss)
- ``power``       mains power lost
- ``trend``       multi-day drift from daily means (the "no threshold catches
                  this" rule)
- ``pattern_defrost``  distilled benign-twin recognizer: a bounded spike with
                  flat humidity and a closed door is a defrost cycle — it
                  *suppresses* the escalation of the rules it names, which is
                  exactly where the post-distillation cloud savings come from.

Invariant I3: ``RuleBundle.verify()`` must pass before ``activate_bundle``
hot-swaps anything; unsigned/invalid/downgraded bundles are refused and the
refusal is chain-logged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

from .canonical import canonical_json
from .crypto import sign as _sign
from .crypto import verify_signature
from .storage import Reading

__all__ = [
    "RULE_TYPES",
    "RuleBundleInvalid",
    "RuleBundleRejected",
    "RuleBundle",
    "Firing",
    "ReflexEngine",
    "builtin_bundle_dict",
    "sign_bundle",
]

RULE_TYPES = ("threshold", "slope", "door_timer", "gap", "power", "trend", "pattern_defrost")
_SEVERITIES = ("info", "watch", "critical")
_ACTIONS = ("sound_alarm", "notify", "annotate_log", "schedule_service")
SCHEMA_ID = "permafrost.rules/v1"


class RuleBundleInvalid(Exception):
    """Bundle fails schema validation."""


class RuleBundleRejected(Exception):
    """Bundle refused at activation time (bad signature / downgrade) — I3."""


# --------------------------------------------------------------------------- bundle


@dataclass(frozen=True)
class RuleBundle:
    """A parsed, schema-valid rule bundle. Signature checks are explicit."""

    version: int
    source: str
    created_ts: float
    rules: list[dict[str, Any]]
    raw: dict[str, Any]

    @classmethod
    def parse(cls, bundle: dict[str, Any] | str) -> "RuleBundle":
        if isinstance(bundle, str):
            try:
                bundle = json.loads(bundle)
            except json.JSONDecodeError as exc:
                raise RuleBundleInvalid(f"bundle is not valid JSON: {exc}") from exc
        if not isinstance(bundle, dict):
            raise RuleBundleInvalid("bundle must be a JSON object")
        if bundle.get("schema") != SCHEMA_ID:
            raise RuleBundleInvalid(f"unknown schema (want {SCHEMA_ID!r})")
        version = bundle.get("version")
        if not isinstance(version, int) or version < 1:
            raise RuleBundleInvalid("version must be a positive integer")
        rules = bundle.get("rules")
        if not isinstance(rules, list) or not rules:
            raise RuleBundleInvalid("rules must be a non-empty list")
        seen_ids: set[str] = set()
        for r in rules:
            cls._validate_rule(r, seen_ids)
        return cls(
            version=version,
            source=str(bundle.get("source", "unknown")),
            created_ts=float(bundle.get("created_ts", 0.0)),
            rules=rules,
            raw=bundle,
        )

    @staticmethod
    def _validate_rule(r: Any, seen_ids: set[str]) -> None:
        if not isinstance(r, dict):
            raise RuleBundleInvalid("each rule must be an object")
        rid, rtype = r.get("id"), r.get("type")
        if not rid or not isinstance(rid, str):
            raise RuleBundleInvalid("rule missing id")
        if rid in seen_ids:
            raise RuleBundleInvalid(f"duplicate rule id {rid!r}")
        seen_ids.add(rid)
        if rtype not in RULE_TYPES:
            raise RuleBundleInvalid(f"rule {rid!r}: unknown type {rtype!r}")
        if r.get("severity") not in _SEVERITIES:
            raise RuleBundleInvalid(f"rule {rid!r}: severity must be one of {_SEVERITIES}")
        actions = r.get("actions")
        if not isinstance(actions, list) or any(a not in _ACTIONS for a in actions):
            raise RuleBundleInvalid(f"rule {rid!r}: actions must be a list drawn from {_ACTIONS}")
        if not isinstance(r.get("escalate"), bool):
            raise RuleBundleInvalid(f"rule {rid!r}: escalate must be a boolean")
        required = {
            "threshold": ("field", "op", "value", "sustain_s"),
            "slope": ("per_min", "window_s"),
            "door_timer": ("max_open_s",),
            "gap": ("max_gap_s",),
            "power": (),
            "trend": ("per_day", "min_days"),
            "pattern_defrost": ("max_peak_delta_c", "max_duration_s", "max_humidity_delta"),
        }[rtype]
        for key in required:
            if key not in r:
                raise RuleBundleInvalid(f"rule {rid!r}: missing {key!r}")

    # -- signing ---------------------------------------------------------

    def signable_bytes(self) -> bytes:
        return canonical_json(self.raw)

    def verify(self, sig_hex: str | None, verify_key_hex: str) -> bool:
        """Ed25519 check — the I3 gate. Missing signature is a hard no."""
        if not sig_hex:
            return False
        return verify_signature(self.signable_bytes(), sig_hex, verify_key_hex)

    def if_then_text(self) -> str:
        """Human-readable IF/THEN rendering (CLI + distill output)."""
        lines = [f"# rules v{self.version} ({self.source})"]
        for r in self.rules:
            cond = {
                "threshold": lambda: f"{r['field']} {r['op']} {r['value']} sustained {r['sustain_s']}s",
                "slope": lambda: f"temp rises >= {r['per_min']}C/min over {r['window_s']}s",
                "door_timer": lambda: f"door open >= {r['max_open_s']}s",
                "gap": lambda: f"telemetry gap > {r['max_gap_s']}s",
                "power": lambda: "mains power lost",
                "trend": lambda: f"daily mean drifts >= {r['per_day']}C/day over >= {r['min_days']}d",
                "pattern_defrost": lambda: (
                    f"spike <= {r['max_peak_delta_c']}C for <= {r['max_duration_s']}s, "
                    f"humidity flat (<= {r['max_humidity_delta']}), door closed"
                ),
            }[r["type"]]()
            then = " + ".join(r["actions"]) + ("" if r["escalate"] else " (no cloud escalation)")
            lines.append(f"IF {cond} THEN {then}  [{r['severity']}] ({r['id']})")
        return "\n".join(lines)


def sign_bundle(bundle: dict[str, Any], signing_seed_hex: str) -> str:
    """Cloud-side helper: Ed25519 signature (hex) over the canonical bundle."""
    return _sign(canonical_json(RuleBundle.parse(bundle).raw), signing_seed_hex)


def builtin_bundle_dict() -> dict[str, Any]:
    """The factory-provisioned v1 bundle shipped inside the package."""
    text = resources.files("permafrost.data").joinpath("rules_v1.json").read_text("utf-8")
    return json.loads(text)


# --------------------------------------------------------------------------- engine


@dataclass
class Firing:
    ts: float
    rule_id: str
    rule_type: str
    severity: str
    actions: list[str]
    escalate: bool
    message: str
    suppressed_by: str | None = None


@dataclass
class _RuleState:
    active: bool = False
    since: float | None = None
    fired: bool = False
    last_true: float | None = None
    last_fired_day_index: int = -1


class ReflexEngine:
    """Evaluates the active bundle over the recent-sample window each tick.

    Edge-triggered with a cooldown: a rule fires once per episode; the episode
    ends after the condition has been false for ``cooldown_s``.
    """

    def __init__(self, bundle: RuleBundle, cooldown_s: float = 300.0):
        self.bundle = bundle
        self.cooldown_s = cooldown_s
        self._state: dict[str, _RuleState] = {}

    def swap_bundle(self, bundle: RuleBundle) -> None:
        """Hot-swap (only ever called after signature verification)."""
        self.bundle = bundle
        self._state.clear()

    # -- per-tick evaluation ----------------------------------------------

    def evaluate(
        self,
        window: list[Reading],
        now: Reading,
        daily_means: list[tuple[str, float]],
        prev_ts: float | None,
    ) -> list[Firing]:
        firings: list[Firing] = []
        suppressions: dict[str, str] = {}

        # recognizers first — they may suppress escalating rules this tick
        for r in self.bundle.rules:
            if r["type"] == "pattern_defrost":
                matched = self._defrost_condition(r, window, now)
                for target in r.get("suppresses", []):
                    if matched:
                        suppressions[target] = r["id"]
                f = self._edge_trigger(r, matched, now.ts)
                if f is not None:
                    firings.append(f)

        for r in self.bundle.rules:
            rtype = r["type"]
            if rtype == "pattern_defrost":
                continue
            cond = self._condition(r, window, now, daily_means, prev_ts)
            firing = self._edge_trigger(r, cond, now.ts)
            if firing is not None:
                if r["id"] in suppressions:
                    firing.escalate = False
                    firing.actions = ["annotate_log"]
                    firing.severity = "info"
                    firing.suppressed_by = suppressions[r["id"]]
                firings.append(firing)
        return firings

    # -- conditions ---------------------------------------------------------

    def _condition(
        self,
        r: dict[str, Any],
        window: list[Reading],
        now: Reading,
        daily_means: list[tuple[str, float]],
        prev_ts: float | None,
    ) -> bool:
        rtype = r["type"]
        if rtype == "threshold":
            return self._threshold(r, window, now)
        if rtype == "slope":
            return self._slope(r, window, now)
        if rtype == "door_timer":
            return self._door_timer(r, window, now)
        if rtype == "gap":
            return prev_ts is not None and (now.ts - prev_ts) > float(r["max_gap_s"])
        if rtype == "power":
            return not now.power_ok
        if rtype == "trend":
            return self._trend(r, daily_means)
        return False  # unknown types never reach here (schema-validated)

    @staticmethod
    def _cmp(op: str, a: float, b: float) -> bool:
        return a > b if op == ">" else a < b

    def _threshold(self, r: dict[str, Any], window: list[Reading], now: Reading) -> bool:
        if not self._cmp(r["op"], now.temp_c, float(r["value"])):
            return False
        sustain = float(r["sustain_s"])
        cutoff = now.ts - sustain
        span_ok = bool(window) and window[0].ts <= cutoff
        if not span_ok:
            return False
        recent = [s for s in window if s.ts >= cutoff]
        return all(self._cmp(r["op"], s.temp_c, float(r["value"])) for s in recent)

    def _slope(self, r: dict[str, Any], window: list[Reading], now: Reading) -> bool:
        cutoff = now.ts - float(r["window_s"])
        candidates = [s for s in window if s.ts >= cutoff]
        if len(candidates) < 2:
            return False
        first = candidates[0]
        dt_min = (now.ts - first.ts) / 60.0
        if dt_min < 1.0:
            return False
        return (now.temp_c - first.temp_c) / dt_min >= float(r["per_min"])

    @staticmethod
    def _door_timer(r: dict[str, Any], window: list[Reading], now: Reading) -> bool:
        if not now.door_open:
            return False
        open_since = now.ts
        for s in reversed(window):
            if not s.door_open:
                break
            open_since = s.ts
        return (now.ts - open_since) >= float(r["max_open_s"])

    @staticmethod
    def _trend(r: dict[str, Any], daily_means: list[tuple[str, float]]) -> bool:
        min_days = int(r["min_days"])
        if len(daily_means) < min_days:
            return False
        first, last = daily_means[0][1], daily_means[-1][1]
        days = len(daily_means) - 1
        return days > 0 and (last - first) >= float(r["per_day"]) * days

    def _defrost_condition(self, r: dict[str, Any], window: list[Reading], now: Reading) -> bool:
        """Current spike episode matches the defrost signature so far."""
        if not window:
            return False
        temps = sorted(s.temp_c for s in window)
        baseline = temps[max(0, int(0.10 * (len(temps) - 1)))]
        if now.temp_c < baseline + 1.0:
            return False  # not in a spike
        # walk back to episode start
        start_ts = now.ts
        episode = [now]
        for s in reversed(window):
            if s.temp_c < baseline + 1.0:
                break
            start_ts = s.ts
            episode.append(s)
        duration = now.ts - start_ts
        if duration > float(r["max_duration_s"]):
            return False
        peak_delta = max(s.temp_c for s in episode) - baseline
        if peak_delta > float(r["max_peak_delta_c"]):
            return False
        if any(s.door_open for s in episode):
            return False
        hums = [s.humidity_pct for s in episode if s.humidity_pct is not None]
        base_hums = [s.humidity_pct for s in window if s.humidity_pct is not None]
        if hums and base_hums:
            base = sorted(base_hums)[max(0, int(0.10 * (len(base_hums) - 1)))]
            if max(hums) - base > float(r["max_humidity_delta"]):
                return False
        return True

    # -- edge trigger / debounce ---------------------------------------------

    def _edge_trigger(self, r: dict[str, Any], cond: bool, now_ts: float) -> Firing | None:
        st = self._state.setdefault(r["id"], _RuleState())
        if cond:
            st.last_true = now_ts
            if not st.active:
                st.active = True
                st.since = now_ts
                st.fired = False
            if not st.fired:
                st.fired = True
                if r["type"] == "trend":
                    # trend conditions stay true for days; re-fire at most daily
                    day_index = int(now_ts // 86400)
                    if st.last_fired_day_index == day_index:
                        return None
                    st.last_fired_day_index = day_index
                return Firing(
                    ts=now_ts,
                    rule_id=r["id"],
                    rule_type=r["type"],
                    severity=r["severity"],
                    actions=list(r["actions"]),
                    escalate=bool(r["escalate"]),
                    message=r.get("message", r["id"]),
                )
        else:
            if st.active and st.last_true is not None and (now_ts - st.last_true) >= self.cooldown_s:
                st.active = False
                st.fired = False
        return None
