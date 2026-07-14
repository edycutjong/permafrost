"""Replay harness — the zero-hardware judging path.

Drives the *identical* EdgeDaemon from a recorded seed curve on the virtual
clock. The ``offline_from``/``online_from`` tick window is the demo's pulled
Ethernet cable; ``tick_limit`` + ``resume`` are the power-cut harness for
invariant I1.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .actions import Activation
from .chain import ChainReport, sign_daily_roots, verify_chain
from .crypto import dev_keys
from .daemon import DaemonConfig, EdgeDaemon, TickResult
from .link import DiagnoserClient, LocalAppLink
from .qwen import default_transport
from .qwen.transport import QwenTransport
from .rules import Firing
from .sampler import CsvSource
from .storage import EdgeStore
from .timeutil import day_of

__all__ = ["ReplayResult", "run_replay", "DEFAULT_FRIDGE_META"]

DEFAULT_FRIDGE_META: dict[str, Any] = {
    "fridge_id": "clinic-fridge-01",
    "model": "LabCool VR-240 (demo fixture)",
    "defrost_spec": "auto-defrost every 6h, approx 14 min",
    "setpoint_c": 4.0,
    "band_c": [2.0, 8.0],
    "vfc_grade": "A",
}


@dataclass
class ReplayResult:
    ticks: int = 0
    db_path: Path | None = None
    firings: list[Firing] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    alarms: list[Activation] = field(default_factory=list)
    notifications: list[Activation] = field(default_factory=list)
    pending_after: int = 0
    pending_peak: int = 0
    synced_total: int = 0
    offline_ticks: int = 0
    heartbeats: int = 0
    rules_version: int = 0
    chain_report: ChainReport | None = None
    roots_signed: list[str] = field(default_factory=list)
    last_ts: float | None = None

    def alarms_during(self, offline_only_window: tuple[float, float]) -> list[Activation]:
        lo, hi = offline_only_window
        return [a for a in self.alarms if lo <= a.ts <= hi]


def _load_fridge_meta(curve_path: Path) -> dict[str, Any]:
    candidate = curve_path.parent / "fridge.json"
    if candidate.exists():
        return json.loads(candidate.read_text("utf-8"))
    return dict(DEFAULT_FRIDGE_META)


def run_replay(
    curve: str | Path,
    db: str | Path,
    *,
    transport: QwenTransport | None = None,
    app: Any | None = None,
    offline_from: int | None = None,
    online_from: int | None = None,
    tick_limit: int | None = None,
    throttle_ms: float = 0.0,
    resume: bool = False,
    reconnect_at_end: bool = True,
    finalize: bool = True,
    initial_bundle: tuple[dict[str, Any], str] | None = None,
    config: DaemonConfig | None = None,
    on_tick: Callable[[int, EdgeDaemon, TickResult], None] | None = None,
    verbose_print: Callable[[str], None] | None = None,
) -> ReplayResult:
    """Run one curve through the full loop. Deterministic under FakeQwen."""
    from .cloud.app import create_app  # local import: cloud dep only where needed

    curve_path = Path(curve)
    db_path = Path(db)
    keys = dev_keys()
    # Default to FakeQwen (deterministic, keyless) UNLESS the operator opted into
    # live mode via PERMAFROST_LIVE=1 + DASHSCOPE_API_KEY — the same switch the
    # README documents. Tests and the judging path never set those, so replay
    # stays byte-deterministic offline.
    transport = transport or default_transport()
    cloud_app = app or create_app(transport)
    link = LocalAppLink(cloud_app, online=True)
    diagnoser = DiagnoserClient(link, keys.sealing_public)

    store = EdgeStore(db_path)
    skip_until = store.last_reading_ts() if resume else None
    source = CsvSource(curve_path, skip_until_ts=skip_until)

    daemon = EdgeDaemon(
        store,
        diagnoser=diagnoser,
        fridge_meta=_load_fridge_meta(curve_path),
        config=config,
    )
    if initial_bundle is not None:
        from .timeutil import VIRTUAL_EPOCH_TS

        bundle, sig = initial_bundle
        daemon.try_activate_bundle(bundle, sig, VIRTUAL_EPOCH_TS)

    result = ReplayResult(db_path=db_path)
    tick = 0
    say = verbose_print or (lambda _msg: None)
    try:
        while True:
            if tick_limit is not None and tick >= tick_limit:
                break
            sample = source.read()
            if sample is None:
                break
            if offline_from is not None and tick == offline_from:
                link.set_online(False)
                say(f"[tick {tick}] ~~ NETWORK CUT — OFFLINE mode, local rules v{daemon.rules_version} protecting ~~")
            if online_from is not None and tick == online_from:
                link.set_online(True)
                say(f"[tick {tick}] ~~ RECONNECTED — syncing queued events ~~")

            tick_result = daemon.process_tick(sample)
            tick += 1
            result.ticks = tick
            result.last_ts = sample.ts
            if not tick_result.online:
                result.offline_ticks += 1
            if tick_result.heartbeat:
                result.heartbeats += 1
            for f in tick_result.firings:
                result.firings.append(f)
                say(
                    f"[tick {tick}] REFLEX {f.severity.upper()} {f.rule_id}: {f.message}"
                    + (f" (suppressed by {f.suppressed_by})" if f.suppressed_by else "")
                )
            for envelope in tick_result.verdicts:
                v = envelope.get("verdict", {})
                say(
                    f"[tick {tick}] VERDICT {v.get('cause')} conf={v.get('confidence')} "
                    f"benign={v.get('benign')} task={envelope.get('task_id')}"
                )
            result.pending_peak = max(result.pending_peak, store.pending_count())
            if on_tick is not None:
                on_tick(tick, daemon, tick_result)
            if throttle_ms > 0:
                time.sleep(throttle_ms / 1000.0)

        if reconnect_at_end and result.last_ts is not None:
            if not link.is_online():
                link.set_online(True)
                say("~~ RECONNECT AT END — flushing offline queue ~~")
            daemon.flush_queue(result.last_ts)

        if finalize and result.last_ts is not None:
            result.roots_signed = sign_daily_roots(
                store, keys.signing_seed, include_partial_day=day_of(result.last_ts)
            )
            result.chain_report = verify_chain(db_path, keys.verify_key)

        result.verdicts = list(daemon.verdicts)
        result.alarms = list(daemon.buzzer.activations)
        result.notifications = list(daemon.notifier.notifications)
        result.pending_after = store.pending_count()
        result.synced_total = store.synced_count()
        result.rules_version = daemon.rules_version
        return result
    finally:
        store.commit()
        store.close()
        link.close()
