"""Alibaba Function Compute 3.0 — MANAGED python runtime entrypoint (EVENT handler).

No container / no ACR: FC installs requirements.txt and invokes this
`handler(event, context)` for the HTTP trigger. FC 3.0 delivers the HTTP request
as a JSON event and expects a {statusCode, headers, body} dict back (a WSGI/ASGI
callable here yields "'FCContext' object is not callable" -> 502, so this is an
EVENT handler, not WSGI).

Permafrost targets py3.12 but does not use enum.StrEnum; the shim below is a
harmless no-op on 3.11+ and a safety net on the 3.10 managed runtime.

Endpoints (anonymous HTTP trigger):
  GET /         service card
  GET /health   liveness -> {"status": "ok"}
  GET /verify   offline graceful-degradation proof: real sockets blocked, uplink
                CUT mid-replay of the committed door_ajar fixture, then RESTORED;
                asserts the local alarm still fires offline, the ECIES queue drains
                on reconnect, and the hash chain + signed daily roots verify.
  GET /run      one deterministic offline replay (FakeQwen, no key) of a seed
                curve (?curve=door_ajar|defrost_cycle|power_loss|compressor_drift)
                -> compact verdict + chain summary.
"""

from __future__ import annotations

import enum
import json
import os
import socket
import sys
import tempfile

# --- 3.10 compat shim: must run before any permafrost import -----------------
if not hasattr(enum, "StrEnum"):
    class StrEnum(str, enum.Enum):  # noqa: D401,UP042 - 3.11 enum.StrEnum polyfill for the 3.10 runtime
        def __str__(self) -> str:
            return str(self.value)
    enum.StrEnum = StrEnum  # type: ignore[attr-defined,assignment,misc]

# The package ships under src/ in the deployed code bundle.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "..", "src"))
if os.path.isdir(_SRC):
    sys.path.insert(0, _SRC)

_SEEDS = os.path.abspath(os.path.join(_HERE, "..", "..", "seeds"))
_CURVES = ("door_ajar", "defrost_cycle", "power_loss", "compressor_drift")

# ticks: cut the uplink before the door opens, restore later (mirrors verify_offline.py)
_OFFLINE_FROM, _ONLINE_FROM = 1700, 2100


def _verify() -> dict:
    """Socket-guarded offline replay of the committed door_ajar fixture.

    Blocks every real TCP connect, replays with the uplink cut mid-run and
    restored later, and asserts the graceful-degradation invariants. The socket
    patch is restored in `finally` so the worker stays healthy for later calls.
    """
    from permafrost.qwen.fake import FakeQwen
    from permafrost.replay import run_replay
    from permafrost.timeutil import VIRTUAL_EPOCH_TS as T0

    curve = os.path.join(_SEEDS, "door_ajar.csv")
    if not os.path.exists(curve):
        return {"error": f"fixture not found at {curve}", "checks": []}

    _orig_connect = socket.socket.connect
    _orig_create = socket.create_connection

    def _blocked(*_a, **_k):  # noqa: ANN001, ANN002, ANN003
        raise OSError("real network access is blocked (offline-first proof)")

    db = os.path.join(tempfile.mkdtemp(), "verify.db")
    try:
        socket.socket.connect = _blocked  # type: ignore[assignment]
        socket.create_connection = _blocked  # type: ignore[assignment]
        result = run_replay(
            curve, db,
            transport=FakeQwen(),
            offline_from=_OFFLINE_FROM,
            online_from=_ONLINE_FROM,
        )
    finally:
        socket.socket.connect = _orig_connect  # type: ignore[assignment]
        socket.create_connection = _orig_create  # type: ignore[assignment]

    lo, hi = T0 + _OFFLINE_FROM * 10, T0 + _ONLINE_FROM * 10
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
    ok = all(passed for _, passed in checks)
    return {
        "overall": "PASS" if ok else "FAIL",
        "fixture": "seeds/door_ajar.csv",
        "real_sockets_blocked": True,
        "offline_alarms": len(offline_alarms),
        "queue_peak": result.pending_peak,
        "synced_total": result.synced_total,
        "offline_ticks": result.offline_ticks,
        "rules_version": result.rules_version,
        "chain": report.summary() if report else None,
        "checks": [{"name": n, "ok": ok_} for n, ok_ in checks],
        "source": "offline FakeQwen replay (deterministic, keyless)",
    }


def _run(curve: str) -> dict:
    """One deterministic offline replay (FakeQwen) of a seed curve."""
    from permafrost.qwen.fake import FakeQwen
    from permafrost.replay import run_replay

    if curve not in _CURVES:
        return {"error": f"unknown curve {curve!r}", "available": list(_CURVES)}
    path = os.path.join(_SEEDS, f"{curve}.csv")
    if not os.path.exists(path):
        return {"error": f"fixture not found at {path}"}

    db = os.path.join(tempfile.mkdtemp(), "run.db")
    result = run_replay(path, db, transport=FakeQwen())
    report = result.chain_report
    verdicts = []
    for env in result.verdicts:
        v = env.get("verdict", {})
        verdicts.append({
            "cause": v.get("cause"),
            "benign": v.get("benign"),
            "confidence": v.get("confidence"),
            "task_id": env.get("task_id"),
            "model": env.get("model"),
        })
    return {
        "curve": curve,
        "transport": "FakeQwen (offline deterministic — no key required)",
        "ticks": result.ticks,
        "reflex_firings": len(result.firings),
        "alarms": len(result.alarms),
        "verdicts": verdicts,
        "rules_version": result.rules_version,
        "chain": report.summary() if report else None,
        "chain_ok": bool(report and report.ok and report.roots_ok),
    }


def _route(path: str, qs: dict) -> tuple[int, dict]:
    path = path.rstrip("/") or "/"
    if path == "/":
        return 200, {
            "service": "permafrost — auditable cold-chain edge brain (Qwen Cloud)",
            "endpoints": {
                "/health": "liveness",
                "/verify": "offline graceful-degradation proof on the committed door_ajar fixture",
                "/run": "one deterministic offline replay (?curve=door_ajar|defrost_cycle|power_loss|compressor_drift)",
            },
            "repo": "https://github.com/edycutjong/permafrost",
        }
    if path == "/health":
        return 200, {"status": "ok"}
    if path == "/verify":
        return 200, _verify()
    if path in ("/run", "/demo"):
        return 200, _run(qs.get("curve", ["door_ajar"])[0])
    return 404, {"error": f"no route {path}"}


def handler(event, context):
    """FC 3.0 event handler for an HTTP trigger.

    `event` is the HTTP request as JSON bytes; return {statusCode, headers, body}.
    """
    from urllib.parse import parse_qs

    try:
        req = json.loads(event) if isinstance(event, (bytes, bytearray, str)) else (event or {})
    except Exception:
        req = {}
    rc_http = (req.get("requestContext") or {}).get("http") or {}
    path = req.get("rawPath") or req.get("path") or rc_http.get("path") or "/"
    qp = req.get("queryParameters") or req.get("queryStringParameters")
    if qp:
        qs = {k: (v if isinstance(v, list) else [v]) for k, v in qp.items()}
    else:
        qs = parse_qs(req.get("rawQueryString", "") or "")
    try:
        code, payload = _route(path, qs)
    except Exception as exc:  # never 500 opaque
        code, payload = 500, {"error": type(exc).__name__, "detail": str(exc)[:400]}
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "isBase64Encoded": False,
        "body": json.dumps(payload, sort_keys=True, indent=2),
    }
