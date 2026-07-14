"""Alibaba Function Compute entrypoint for the Permafrost cloud brain.

This is a thin shim: the whole brain is the FastAPI app in
``permafrost.cloud.app`` — the SAME app the offline judging path mounts
in-process via ``LocalAppLink``. There is no cloud-only code path, so what the
tests exercise is exactly what deploys.

Deploy shape (FC 3.0 **Custom Runtime**, the clean way to run ASGI on FC):
``s.yaml`` sets this file as the bootstrap; FC injects the listen port as
``FC_SERVER_PORT`` (default 9000) and the secrets as environment variables:

    DASHSCOPE_API_KEY          -> live Qwen (else FakeQwen; PERMAFROST_LIVE=1 to enable)
    PERMAFROST_SIGNING_SEED    -> Ed25519 seed that signs rule bundles + Merkle roots
    PERMAFROST_SEALING_PRIVATE -> X25519 private key that opens ECIES event batches

``create_app`` already reads those from the environment, so this file only has
to build the app and serve it.

STATUS: deploy + console recording pending (see PROOF.md). The app runs locally
and in-process today; this shim is written and import-clean.
"""

from __future__ import annotations

import os

from permafrost.cloud.app import create_app

# The ASGI application FC (or `uvicorn infra.fc.handler:app`) serves.
app = create_app()


def main() -> None:
    """Custom-runtime bootstrap: serve the ASGI app on the FC-provided port."""
    import uvicorn

    port = int(os.environ.get("FC_SERVER_PORT", "9000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
