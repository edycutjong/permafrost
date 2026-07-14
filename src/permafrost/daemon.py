"""EdgeDaemon — sense, reflex, queue, sync, log. Identical in replay and hardware mode.

Per 10s tick:
1. gap + door/power transition detection (events + chain)
2. reading -> ring buffer (SQLite WAL) + hash chain
3. reflex rules over the hot window (<100 ms, works with zero connectivity)
4. firings -> buzzer/notify + chain; escalating firings -> ECIES-sealed event
   batch into the offline queue
5. if the link is up: flush queue -> verdicts -> typed actions -> chain
6. heartbeat every 4h (virtual clock in replay)

The chain is committed every tick, so a power cut mid-tick loses at most the
uncommitted tick and never breaks the chain (invariant I1). Rule bundles hot-
swap only through :meth:`try_activate_bundle`, which refuses anything unsigned
or downgraded (invariant I3) and logs the refusal.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .canonical import canonical_json

from .actions import ActionDispatcher, BuzzerSink, NotifierSink
from .chain import ChainLogger
from .crypto import dev_keys
from .link import DiagnoserClient, OfflineError
from .rules import Firing, ReflexEngine, RuleBundle, RuleBundleInvalid, RuleBundleRejected, builtin_bundle_dict, sign_bundle
from .storage import EdgeStore, Reading

__all__ = ["DaemonConfig", "TickResult", "EdgeDaemon", "activate_bundle_on_store"]


@dataclass(frozen=True)
class DaemonConfig:
    heartbeat_s: float = 4 * 3600.0
    curve_window_s: float = 6 * 3600.0
    max_curve_points: int = 360
    ring_window_s: float = 24 * 3600.0
    hot_window_s: float = 1800.0
    gap_event_s: float = 120.0
    evict_every_ticks: int = 100


@dataclass
class TickResult:
    ts: float
    firings: list[Firing] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    escalated: int = 0
    heartbeat: bool = False
    gap_s: float | None = None
    online: bool = False


def _downsample(rows: list[Reading], max_points: int) -> list[dict[str, Any]]:
    stride = max(1, -(-len(rows) // max_points))  # ceil division
    picked = rows[::stride]
    if rows and picked[-1].ts != rows[-1].ts:
        picked.append(rows[-1])
    return [
        {
            "ts": r.ts,
            "temp_c": r.temp_c,
            "humidity_pct": r.humidity_pct,
            "door_open": r.door_open,
            "power_ok": r.power_ok,
        }
        for r in picked
    ]


def activate_bundle_on_store(
    store: EdgeStore,
    chain: ChainLogger,
    bundle: dict[str, Any] | str,
    sig_hex: str | None,
    verify_key_hex: str,
    now_ts: float,
) -> RuleBundle:
    """The I3 gate, usable by daemon and CLI alike.

    Order matters: schema-parse, **verify signature**, check version monotonic —
    only then persist + return the bundle for hot-swap. Every refusal is
    chain-logged before the exception propagates.
    """

    def _refuse(reason: str) -> RuleBundleRejected:
        chain.append(now_ts, "rule_bundle_rejected", {"reason": reason})
        store.commit()
        return RuleBundleRejected(reason)

    try:
        parsed = RuleBundle.parse(bundle)
    except RuleBundleInvalid as exc:
        raise _refuse(f"schema invalid: {exc}") from exc

    if not parsed.verify(sig_hex, verify_key_hex):
        raise _refuse(
            f"signature {'missing' if not sig_hex else 'invalid'} for bundle v{parsed.version}"
        )

    active = store.active_rules()
    current_version = active[0] if active else 0
    if parsed.version <= current_version:
        raise _refuse(f"downgrade refused: v{parsed.version} <= active v{current_version}")

    store.store_rules(parsed.version, json.dumps(parsed.raw, sort_keys=True), sig_hex or "", parsed.source, now_ts)
    chain.append(now_ts, "rules_activated", {"version": parsed.version, "source": parsed.source})
    store.commit()
    return parsed


class EdgeDaemon:
    """One fridge, one loop: sense -> reflex -> (cloud) -> act -> chained log."""

    def __init__(
        self,
        store: EdgeStore,
        *,
        diagnoser: DiagnoserClient | None = None,
        fridge_meta: dict[str, Any] | None = None,
        rules_verify_key: str | None = None,
        config: DaemonConfig | None = None,
        buzzer: BuzzerSink | None = None,
        notifier: NotifierSink | None = None,
        on_verdict: Callable[[dict[str, Any]], None] | None = None,
    ):
        keys = dev_keys()
        self.store = store
        self.chain = ChainLogger(store)
        self.cfg = config or DaemonConfig()
        self.diagnoser = diagnoser
        self.fridge_meta = fridge_meta or {"fridge_id": "clinic-fridge-01"}
        self.verify_key = rules_verify_key or keys.verify_key
        self.buzzer = buzzer or BuzzerSink()
        self.notifier = notifier or NotifierSink()
        self.dispatcher = ActionDispatcher(self.buzzer, self.notifier)
        self.on_verdict = on_verdict
        self.verdicts: list[dict[str, Any]] = []

        # rules: load active bundle or factory-provision the builtin v1
        active = store.active_rules()
        if active is None:
            from .timeutil import VIRTUAL_EPOCH_TS

            bundle_dict = builtin_bundle_dict()
            sig = sign_bundle(bundle_dict, keys.signing_seed)  # factory provisioning (dev demo key)
            parsed = activate_bundle_on_store(
                store, self.chain, bundle_dict, sig, self.verify_key, VIRTUAL_EPOCH_TS
            )
        else:
            parsed = RuleBundle.parse(active[1])
        self.engine = ReflexEngine(parsed)

        # hot window + previous sample survive restarts by reloading from the DB
        self._window: deque[Reading] = deque()
        last_ts = store.last_reading_ts()
        if last_ts is not None:
            for r in store.readings_since(last_ts - self.cfg.hot_window_s):
                self._window.append(r)
        self._prev: Reading | None = self._window[-1] if self._window else None
        self._online_state: bool | None = None
        self._tick_count = 0

    # ------------------------------------------------------------------ properties

    @property
    def rules_version(self) -> int:
        return self.engine.bundle.version

    # ------------------------------------------------------------------ tick

    def process_tick(self, r: Reading) -> TickResult:
        cfg = self.cfg
        result = TickResult(ts=r.ts)
        prev = self._prev

        # gap detection (power cut / sensor loss while the daemon was dark)
        if prev is not None and (r.ts - prev.ts) > cfg.gap_event_s:
            result.gap_s = r.ts - prev.ts
            self.store.add_event(r.ts, "gap", {"gap_s": result.gap_s, "since_ts": prev.ts})
            self.chain.append(r.ts, "gap", {"gap_s": result.gap_s, "since_ts": prev.ts})

        # door / power transitions
        if prev is None or prev.door_open != r.door_open:
            payload = {"door_open": r.door_open}
            self.store.add_event(r.ts, "door", payload)
            self.chain.append(r.ts, "door", payload)
        if prev is None or prev.power_ok != r.power_ok:
            payload = {"power_ok": r.power_ok}
            self.store.add_event(r.ts, "power", payload)
            self.chain.append(r.ts, "power", payload)

        # ring buffer + chain
        self.store.add_reading(r)
        self.chain.append(
            r.ts,
            "reading",
            {
                "temp_c": r.temp_c,
                "humidity_pct": r.humidity_pct,
                "door_open": r.door_open,
                "power_ok": r.power_ok,
                "probe": r.probe,
            },
        )
        self._window.append(r)
        while self._window and self._window[0].ts < r.ts - cfg.hot_window_s:
            self._window.popleft()
        self._tick_count += 1
        if self._tick_count % cfg.evict_every_ticks == 0:
            self.store.evict_ring(r.ts, cfg.ring_window_s)

        # reflex tier (works with zero connectivity)
        window = list(self._window)[:-1]
        firings = self.engine.evaluate(window, r, self.store.daily_means(), prev.ts if prev else None)
        for firing in firings:
            self._handle_firing(firing, r)
            result.firings.append(firing)
            if firing.escalate:
                result.escalated += 1

        # heartbeat (virtual clock)
        last_hb = self.store.meta_get("last_heartbeat_ts")
        if last_hb is None:
            self.store.meta_set("last_heartbeat_ts", str(r.ts))
        elif r.ts - float(last_hb) >= cfg.heartbeat_s:
            self.store.meta_set("last_heartbeat_ts", str(r.ts))
            self.chain.append(
                r.ts,
                "heartbeat",
                {"rules_version": self.rules_version, "pending_batches": self.store.pending_count()},
            )
            result.heartbeat = True

        # link state + queue flush
        online = bool(self.diagnoser and self.diagnoser.is_online())
        result.online = online
        if self._online_state is None or online != self._online_state:
            self.chain.append(r.ts, "link_state", {"online": online})
            self._online_state = online
        if online and self.store.pending_count() > 0:
            result.verdicts.extend(self.flush_queue(r.ts))

        self._prev = r
        self.store.commit()
        return result

    # ------------------------------------------------------------------ firings & escalation

    def _handle_firing(self, firing: Firing, now: Reading) -> None:
        self.chain.append(
            firing.ts,
            "reflex",
            {
                "rule_id": firing.rule_id,
                "severity": firing.severity,
                "message": firing.message,
                "escalate": firing.escalate,
                "suppressed_by": firing.suppressed_by,
                "rules_version": self.rules_version,
            },
        )
        for tool in firing.actions:
            if tool == "annotate_log":
                self.chain.append(firing.ts, "annotation", {"source": "reflex", "note": firing.message, "rule_id": firing.rule_id})
            else:
                self.dispatcher.dispatch(firing.ts, tool, "reflex", {"rule_id": firing.rule_id, "message": firing.message})
        if firing.escalate:
            self._enqueue_excursion(firing, now)

    def _enqueue_excursion(self, firing: Firing, now: Reading) -> None:
        payload = self._excursion_payload(firing, now)
        digest = hashlib.sha256(canonical_json(payload)).hexdigest()
        if self.diagnoser is not None:
            sealed = self.diagnoser.seal_payload(payload)  # ECIES to the cloud pubkey
        else:
            sealed = b"unsent:" + digest.encode()
        self.store.enqueue(now.ts, digest, sealed)
        self.chain.append(
            now.ts,
            "excursion_queued",
            {"digest": digest, "trigger": firing.rule_id, "severity": firing.severity},
        )

    def _excursion_payload(self, firing: Firing, now: Reading) -> dict[str, Any]:
        since = now.ts - self.cfg.curve_window_s
        rows = self.store.readings_since(since)
        return {
            "fridge_meta": self.fridge_meta,
            "curve": _downsample(rows, self.cfg.max_curve_points),
            "door_events": self.store.events_since(since, kinds=["door"]),
            "power_events": self.store.events_since(since, kinds=["power", "gap"]),
            "daily_means": [[d, m] for d, m in self.store.daily_means()],
            "trigger": {
                "rule_id": firing.rule_id,
                "severity": firing.severity,
                "message": firing.message,
                "ts": firing.ts,
            },
        }

    # ------------------------------------------------------------------ queue flush (reconnect sync)

    def flush_queue(self, now_ts: float) -> list[dict[str, Any]]:
        """Send every pending sealed batch; stop cleanly on offline. Returns verdict envelopes."""
        if self.diagnoser is None:
            return []
        delivered: list[dict[str, Any]] = []
        for queue_id, digest, sealed in self.store.pending_batches():
            try:
                envelope = self.diagnoser.diagnose_sealed(sealed)
            except OfflineError:
                break
            verdict = envelope.get("verdict", {})
            self.chain.append(
                now_ts,
                "verdict",
                {
                    "verdict": verdict,
                    "task_id": envelope.get("task_id", ""),
                    "model": envelope.get("model", ""),
                    "batch_digest": digest,
                },
            )
            self._execute_verdict_actions(verdict, now_ts)
            self.store.mark_synced(queue_id, now_ts)
            delivered.append(envelope)
            self.verdicts.append(envelope)
            if self.on_verdict:
                self.on_verdict(envelope)
        if delivered:
            self.chain.append(now_ts, "sync", {"batches": len(delivered)})
            self.store.commit()
        return delivered

    def _execute_verdict_actions(self, verdict: dict[str, Any], now_ts: float) -> None:
        for action in verdict.get("actions", []):
            tool = action.get("tool", "")
            detail = {k: v for k, v in action.items() if k != "tool"}
            detail["cause"] = verdict.get("cause")
            if tool == "annotate_log":
                self.chain.append(now_ts, "annotation", {"source": "verdict", **detail})
            elif tool == "update_edge_rules":
                # bundles only ever arrive through the signed /distill path
                self.chain.append(now_ts, "annotation", {"source": "verdict", "note": "rules update proposed"})
            else:
                self.dispatcher.dispatch(now_ts, tool, "verdict", detail)

    # ------------------------------------------------------------------ signed rule hot-swap (I3)

    def try_activate_bundle(self, bundle: dict[str, Any] | str, sig_hex: str | None, now_ts: float) -> bool:
        """Verify -> persist -> hot-swap. Raises RuleBundleRejected on refusal."""
        parsed = activate_bundle_on_store(self.store, self.chain, bundle, sig_hex, self.verify_key, now_ts)
        self.engine.swap_bundle(parsed)
        return True
