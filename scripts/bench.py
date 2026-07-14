#!/usr/bin/env python3
"""Permafrost bench — the numbers behind the claims (SPEC §10, COMPLEXITY §5).

Thin runner around ``permafrost.benchmark.run_all`` (where the logic lives). It:

1. runs the **4-class confusion matrix x10** (deterministic FakeQwen) and asserts
   cause-classification **accuracy >= 0.9**,
2. measures **local reflex latency** p50/p95 and asserts **p95 < 100 ms**,
3. measures **$/day before vs after distillation** and asserts **>= 60%** cloud
   spend saved with **detection preserved** on the door-ajar control day,

then writes the markdown table to ``docs/BENCH.md``.

Offline, keyless, no network: every verdict is FakeQwen. Exit 1 if any floor fails.

    python scripts/bench.py            # full (10 runs / 200 latency iters)
    python scripts/bench.py --quick    # CI smoke (3 runs / 50 iters)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # run from a checkout without installing

from permafrost.benchmark import run_all  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="fewer runs (CI smoke)")
    ap.add_argument("--seeds", default=str(ROOT / "seeds"), help="seed curves directory")
    ap.add_argument("--workdir", default=str(ROOT / "out" / "bench"), help="scratch dir for replays")
    ap.add_argument("--out", default=str(ROOT / "docs" / "BENCH.md"), help="markdown report path")
    args = ap.parse_args(argv)

    md, ok, data = run_all(args.seeds, args.workdir, quick=args.quick)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md + "\n")

    print(md)
    print(f"\nwrote {out}")
    cm, lat, cost = data["confusion"], data["latency"], data["cost"]
    print(
        f"accuracy={cm['accuracy']:.3f} (>=0.9) | reflex_p95={lat['reflex_p95_ms']:.3f}ms (<100) | "
        f"savings={cost['savings_pct']:.0f}% (>=60) | detection_kept={cost['detection_kept']}"
    )
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
