"""CloudLink — how the edge reaches the brain, and how we cut the cable.

- ``LocalAppLink`` mounts the FastAPI cloud app **in-process** (ASGI transport,
  zero real sockets) — the offline-first judging path. Its ``online`` flag is
  the demo's Ethernet cable: flip it mid-replay and the daemon degrades
  gracefully; flip it back and the queue syncs.
- ``HttpLink`` talks to a deployed Function Compute endpoint (same wire
  format). Not exercised in tests (FC deployment pending — see README Status).
"""

from __future__ import annotations

import base64
import warnings
from typing import Any, Protocol

import httpx

__all__ = [
    "OfflineError",
    "CloudLink",
    "LocalAppLink",
    "HttpLink",
    "DiagnoserClient",
    "make_inprocess_client",
]


def make_inprocess_client(app: Any, base_url: str = "http://permafrost.cloud.local") -> Any:
    """Sync in-process client over the ASGI app — zero real sockets."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*httpx2.*")
        from fastapi.testclient import TestClient
    return TestClient(app, base_url=base_url)


class OfflineError(Exception):
    """The link is down; callers queue and retry on reconnect."""


class CloudLink(Protocol):
    def is_online(self) -> bool: ...

    def diagnose_sealed(self, sealed: bytes) -> dict[str, Any]:
        """POST a sealed event batch to /diagnose; returns the verdict envelope."""
        ...


class LocalAppLink:
    """In-process ASGI client around the cloud app (no network, ever)."""

    def __init__(self, app: Any, online: bool = True):
        self._client = make_inprocess_client(app)
        self.online = online

    def set_online(self, online: bool) -> None:
        self.online = online

    def is_online(self) -> bool:
        return self.online

    def diagnose_sealed(self, sealed: bytes) -> dict[str, Any]:
        if not self.online:
            raise OfflineError("link is offline")
        resp = self._client.post("/diagnose", json={"sealed_b64": base64.b64encode(sealed).decode()})
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()


class HttpLink:
    """Real HTTPS link to a deployed cloud endpoint (Function Compute)."""

    def __init__(self, base_url: str, timeout_s: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout_s)

    def is_online(self) -> bool:
        try:
            return self._client.get("/healthz").status_code == 200
        except httpx.HTTPError:
            return False

    def diagnose_sealed(self, sealed: bytes) -> dict[str, Any]:
        try:
            resp = self._client.post("/diagnose", json={"sealed_b64": base64.b64encode(sealed).decode()})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise OfflineError(str(exc)) from exc


class DiagnoserClient:
    """Edge-side client: seal an event payload to the cloud pubkey, send, return verdict.

    This is the only path an event batch ever takes off the device — always
    ECIES-sealed (COMPLEXITY §2 payload envelope).
    """

    def __init__(self, link: CloudLink, cloud_sealing_public_hex: str):
        from .canonical import canonical_json
        from .crypto import seal

        self._link = link
        self._pub = cloud_sealing_public_hex
        self._seal = seal
        self._canon = canonical_json

    def seal_payload(self, payload: dict[str, Any]) -> bytes:
        return self._seal(self._canon(payload), self._pub)

    def is_online(self) -> bool:
        return self._link.is_online()

    def diagnose(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._link.diagnose_sealed(self.seal_payload(payload))

    def diagnose_sealed(self, sealed: bytes) -> dict[str, Any]:
        return self._link.diagnose_sealed(sealed)
