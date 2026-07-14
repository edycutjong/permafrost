#!/usr/bin/env python3
"""verify_offline.py — the track's "graceful degradation" criterion as a CI gate.

Installs a hard **socket guard** (any real TCP ``connect`` raises), proving the
whole loop touches zero real network, then replays the ``door_ajar`` curve with
the (virtual) uplink CUT mid-run and RESTORED later — the demo's pulled Ethernet
cable. It asserts:

  (a) the local reflex **alarm still fires** while the network is down,
  (b) the ECIES-sealed event **queue grows** while offline,
  (c) after reconnect the queue **drains** and the hash chain **verifies
      gap-free** (signed daily Merkle roots included).

Exit 0 iff all hold. Deterministic, offline, keyless — no DASHSCOPE_API_KEY,
no hardware.

    python scripts/verify_offline.py
"""

from __future__ import annotations

import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

OFFLINE_FROM, ONLINE_FROM = 1700, 2100  # ticks: cut before the door opens, restore later


class NetworkKilled(OSError):
    """Raised if anything attempts a real outbound socket connection."""


def install_socket_guard() -> None:
    """Make every real TCP connect fail. The in-process ASGI link uses no sockets,
    so the full replay still runs — that IS the offline-first proof."""

    def _blocked_connect(self, *args, **kwargs):  # noqa: ANN001
        raise NetworkKilled("real network access is blocked (offline-first proof)")

    def _blocked_create_connection(*args, **kwargs):  # noqa: ANN001
        raise NetworkKilled("real network access is blocked (offline-first proof)")

    socket.socket.connect = _blocked_connect  # type: ignore[assignment]
    socket.create_connection = _blocked_create_connection  # type: ignore[assignment]


def main() -> int:
    install_socket_guard()

    from permafrost.qwen.fake import FakeQwen
    from permafrost.replay import run_replay
    from permafrost.timeutil import VIRTUAL_EPOCH_TS as T0

    seeds = ROOT / "seeds"
    db = Path(tempfile.mkdtemp()) / "verify_offline.db"

    result = run_replay(
        seeds / "door_ajar.csv",
        db,
        transport=FakeQwen(),
        offline_from=OFFLINE_FROM,
        online_from=ONLINE_FROM,
    )

    lo, hi = T0 + OFFLINE_FROM * 10, T0 + ONLINE_FROM * 10
    offline_alarms = result.alarms_during((lo, hi))
    report = result.chain_report

    checks = [
        ("local reflex alarm fired WHILE OFFLINE", len(offline_alarms) >= 1),
        ("ECIES event queue grew while offline", result.pending_peak >= 1),
        ("queue drained after reconnect", result.pending_after == 0),
        ("all queued batches synced", result.synced_total >= 1),
        ("hash chain verifies gap-free after reconnect", bool(report and report.ok)),
        ("signed daily Merkle roots verify", bool(report and report.roots_ok)),
        ("offline ticks were actually exercised", result.offline_ticks > 0),
    ]

    print("verify_offline — real sockets blocked; network CUT mid-replay (door_ajar)\n")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed

    print(
        f"\n  offline alarms: {len(offline_alarms)} | queue peak: {result.pending_peak} | "
        f"synced: {result.synced_total} | offline ticks: {result.offline_ticks} | "
        f"rules v{result.rules_version}"
    )
    if report is not None:
        print(f"  chain: {report.summary()}")
    print(f"\n{'OK — graceful degradation verified (exit 0)' if ok else 'FAIL — degradation broken (exit 1)'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
