"""The cloud brain as a FastAPI app.

Runs three ways with the same code:
- in-process via ``LocalAppLink`` (offline judging path — zero sockets)
- locally: ``uvicorn "permafrost.cloud.app:create_default_app" --factory``
- on Alibaba Function Compute behind an HTTPS trigger (see /cloud + /infra/fc;
  deployment pending — README Status).

Endpoints: ``POST /diagnose`` · ``POST /distill`` · ``GET /report/weekly`` ·
``GET /healthz``.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

from .. import __version__
from ..crypto import SignatureInvalid, dev_keys, unseal
from ..qwen import default_transport
from ..qwen.transport import QwenTransport
from .diagnose import DiagnoseRequest, diagnose
from .distill import DistillRequest, distill
from .guidance import GuidanceStore
from .report import weekly_report_markdown

__all__ = ["create_app", "create_default_app"]


def create_app(
    transport: QwenTransport | None = None,
    *,
    signing_seed_hex: str | None = None,
    sealing_private_hex: str | None = None,
) -> FastAPI:
    """App factory. Defaults: FakeQwen transport + deterministic dev demo keys.

    Production (FC) injects real secrets via environment:
    ``PERMAFROST_SIGNING_SEED`` / ``PERMAFROST_SEALING_PRIVATE`` (hex).
    """
    keys = dev_keys()
    transport = transport or default_transport()
    signing_seed = signing_seed_hex or os.environ.get("PERMAFROST_SIGNING_SEED") or keys.signing_seed
    sealing_private = (
        sealing_private_hex or os.environ.get("PERMAFROST_SEALING_PRIVATE") or keys.sealing_private
    )

    app = FastAPI(title="Permafrost cloud brain", version=__version__)
    app.state.transport = transport
    app.state.guidance = GuidanceStore(transport)
    app.state.history = []  # verdict envelopes, in arrival order

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "service": "permafrost-cloud", "verdicts_seen": len(app.state.history)}

    @app.post("/diagnose")
    async def diagnose_endpoint(request: Request) -> dict[str, Any]:
        body = await request.json()
        if "sealed_b64" in body:
            try:
                plaintext = unseal(base64.b64decode(body["sealed_b64"]), sealing_private)
                payload = json.loads(plaintext)
            except (SignatureInvalid, ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=400, detail=f"sealed batch rejected: {exc}") from exc
        else:
            payload = body
        try:
            req = DiagnoseRequest.model_validate(payload)
        except Exception as exc:  # pydantic ValidationError -> 422 semantics
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            result = diagnose(req, app.state.transport, app.state.guidance)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        envelope = {
            "verdict": result.verdict,
            "task_id": result.task_id,
            "model": result.model,
            "thinking": result.thinking,
            "guidance_ids": result.guidance_ids,
        }
        history_row = {**result.verdict, "task_id": result.task_id}
        curve = req.curve
        if curve:
            history_row["ts"] = float(curve[-1].get("ts", 0.0))
        app.state.history.append(history_row)
        return envelope

    @app.post("/distill")
    async def distill_endpoint(request: Request) -> dict[str, Any]:
        body = await request.json()
        payload = dict(body) if isinstance(body, dict) else {}
        if not payload.get("verdicts"):
            payload["verdicts"] = list(app.state.history)
        try:
            req = DistillRequest.model_validate(payload)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            result = distill(req, app.state.transport, signing_seed)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return result.model_dump()

    @app.get("/report/weekly")
    def report_endpoint(week: int = Query(ge=1, le=53)) -> dict[str, Any]:
        md = weekly_report_markdown(week, list(app.state.history))
        return {"week": week, "verdicts": len(app.state.history), "markdown": md}

    return app


def create_default_app() -> FastAPI:
    """Uvicorn --factory entrypoint."""
    return create_app()
