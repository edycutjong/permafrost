# Alibaba proof — deployment evidence checklist

Separate from the demo video (PRODUCTION_PLAN.md). This is the "it really ran on
Alibaba" evidence a judge can click. **Status: PENDING** — the app runs locally and
in-process today; the deploy + recording below are not yet captured. Nothing in this
repo claims otherwise (see README Status).

## Why the deploy is low-risk

The Function Compute handler (`handler.py`) serves the **exact** FastAPI app the
offline test suite exercises (`permafrost.cloud.app`). There is no cloud-only code
path, so the 331 passing tests already cover the deployed behaviour; deployment is a
packaging + secrets step, not new logic.

## Deploy

```bash
npm i -g @serverless-devs/s
s config add                                  # Alibaba Cloud AK/SK
export DASHSCOPE_API_KEY=...                   # live Qwen
export PERMAFROST_SIGNING_SEED=$(python -c "from permafrost.crypto import generate_signing_seed as g; print(g())")
export PERMAFROST_SEALING_PRIVATE=$(python -c "from permafrost.crypto import generate_sealing_private as g; print(g())")
s deploy                                       # -> prints the HTTPS trigger URL
```

## Evidence to capture (check each when done)

- [ ] **FC console — function overview** screenshot: `permafrost-brain`, Custom Runtime,
      region `ap-southeast-1`, HTTP trigger URL visible.
- [ ] **Live health check** against the deployed URL:
      `curl https://<fc-url>/healthz` → `{"ok": true, ...}` (paste the response).
- [ ] **Live diagnosis** end-to-end with a real Qwen task id:
      `curl -X POST https://<fc-url>/diagnose -d @seeds/door_ajar.event.json`
      → verdict envelope whose `task_id` is a **real DashScope id** (not `fake-*`).
- [ ] **FC invocation logs** (SLS) screenshot showing the `/diagnose` request + the
      `qwen3.7-plus` upstream call latency.
- [ ] **30–60 s console recording**: an invocation, the logs, and the OSS object below.
- [ ] **OSS signed root object**: `oss://permafrost-coldchain/roots/clinic-fridge-01/2026-01-05.json`
      — the Ed25519-signed daily Merkle root, immutable + timestamped + judge-linkable.
      Verify it re-derives: `permafrost verify-chain audit.db` reproduces the same root.

## Real-transaction equivalent

Live Qwen **task-ids in verdict cards** + **OSS-archived signed Merkle roots** are the
immutable, timestamped, judge-linkable artifacts that stand in for an on-chain tx: a
root cannot be forged without the FC-held signing seed, and any edit to the underlying
log makes `verify-chain` fail (invariant I2).

## Secrets hygiene

- Private keys (`PERMAFROST_SIGNING_SEED`, `PERMAFROST_SEALING_PRIVATE`) live **only** in
  the FC environment; the edge ships the public verify key and the cloud sealing pubkey.
- `.gitignore` excludes `*.pem`, `*.key`, `.env*`. The deterministic **dev demo keys**
  (`permafrost.crypto.dev_keys`) are clearly labelled DEMO-ONLY and exist so judges can
  reproduce every byte offline — they are not production secrets.
