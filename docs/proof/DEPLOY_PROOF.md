# Permafrost — Alibaba Function Compute deploy proof

Live, keyless, deterministic — the offline judging path (FakeQwen) served from a
real Alibaba Cloud Function Compute 3.0 managed-python function.

- **Live URL:** https://permafrost-hoznckcsox.ap-southeast-1.fcapp.run
- **Region:** ap-southeast-1
- **Runtime:** FC 3.0 managed `python3.10` (no container / no ACR), event handler `infra.fc.wsgi.handler`
- **Function:** `permafrost` (functionArn `acs:fc:ap-southeast-1:5640684230009202:functions/permafrost`)
- **Trigger:** anonymous HTTP (GET+POST)
- **Deployed:** 2026-07-19

## Endpoints

| Route | What it does |
|---|---|
| `GET /health` | liveness |
| `GET /verify` | offline graceful-degradation proof: real sockets blocked, uplink CUT mid-replay of the committed `seeds/door_ajar.csv` fixture then RESTORED; asserts the local alarm fires offline, the ECIES queue drains on reconnect, and the hash chain + signed daily Merkle roots verify |
| `GET /run` | one deterministic offline replay (FakeQwen, no key); `?curve=door_ajar\|defrost_cycle\|power_loss\|compressor_drift` |

## Live curl outputs

### `curl https://permafrost-hoznckcsox.ap-southeast-1.fcapp.run/health`
```json
{
  "status": "ok"
}
```

### `curl https://permafrost-hoznckcsox.ap-southeast-1.fcapp.run/verify`
```json
{
  "chain": "OK \u2014 2178 entries re-derived, chain intact; 1 signed daily root(s) verified",
  "checks": [
    {
      "name": "local reflex alarm fired WHILE OFFLINE",
      "ok": true
    },
    {
      "name": "ECIES event queue grew while offline",
      "ok": true
    },
    {
      "name": "queue drained after reconnect",
      "ok": true
    },
    {
      "name": "all queued batches synced",
      "ok": true
    },
    {
      "name": "hash chain verifies gap-free after reconnect",
      "ok": true
    },
    {
      "name": "signed daily Merkle roots verify",
      "ok": true
    },
    {
      "name": "offline ticks were actually exercised",
      "ok": true
    }
  ],
  "fixture": "seeds/door_ajar.csv",
  "offline_alarms": 3,
  "offline_ticks": 400,
  "overall": "PASS",
  "queue_peak": 2,
  "real_sockets_blocked": true,
  "rules_version": 1,
  "source": "offline FakeQwen replay (deterministic, keyless)",
  "synced_total": 2
}
```

### `curl https://permafrost-hoznckcsox.ap-southeast-1.fcapp.run/run`
```json
{
  "alarms": 3,
  "chain": "OK \u2014 2177 entries re-derived, chain intact; 1 signed daily root(s) verified",
  "chain_ok": true,
  "curve": "door_ajar",
  "reflex_firings": 2,
  "rules_version": 1,
  "ticks": 2160,
  "transport": "FakeQwen (offline deterministic \u2014 no key required)",
  "verdicts": [
    {
      "benign": false,
      "cause": "door_ajar",
      "confidence": 0.93,
      "model": "qwen3.7-plus",
      "task_id": "fake-00b33c289327"
    },
    {
      "benign": false,
      "cause": "door_ajar",
      "confidence": 0.93,
      "model": "qwen3.7-plus",
      "task_id": "fake-c543db651414"
    }
  ]
}
```

All three returned HTTP 200. `/verify` overall = **PASS** (7/7 invariant checks);
`/run` chain_ok = **true**. No live Qwen key is used — the deployed function runs
the byte-deterministic FakeQwen path (no `PERMAFROST_LIVE` / `DASHSCOPE_API_KEY`
in the function environment).
