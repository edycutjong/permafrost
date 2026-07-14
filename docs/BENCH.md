# Permafrost bench

- Verdict source: FakeQwen (deterministic, offline). Live-model matrix: run with `PERMAFROST_LIVE=1`.
- Confusion matrix: 4 classes x 10 runs — **accuracy 1.000** (floor 0.9)

## Cause classification (rows = truth, cols = predicted)

| truth \ predicted | compressor_degradation | defrost_cycle | door_ajar | power_loss |
|---|---|---|---|---|
| door_ajar | 0 | 0 | 10 | 0 |
| defrost_cycle | 0 | 10 | 0 | 0 |
| compressor_drift | 10 | 0 | 0 | 0 |
| power_loss | 0 | 0 | 0 | 10 |

## Latency

| tier | p50 | p95 | budget |
|---|---|---|---|
| local reflex (rules engine) | 0.006 ms | 0.007 ms | < 100 ms |
| cloud round-trip (in-process app*) | 4.7 ms | 35.8 ms | n/a |

\* in-process ASGI — real FC RTT pending deployment (see README Status).

## $/day — before vs after distillation (measured, estimated list prices)

| phase | cloud diagnose calls / day | est. $/day |
|---|---|---|
| pre-distill (rules v1) | 4 | $0.0136 |
| post-distill (rules v2) | 0 | $0.0000 |

- one-off distillation cost: $0.00007 (qwen3.6-flash)
- **cloud spend savings: 100%** (target >= 60%)
- detection preserved on door-ajar control day: **YES** (alarm fired + door_ajar verdict under distilled rules)

**Overall: PASS**
