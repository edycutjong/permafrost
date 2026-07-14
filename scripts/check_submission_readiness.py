#!/usr/bin/env python3
"""check_submission_readiness.py — one command that says "is Permafrost submittable?".

Runs three families of checks and exits 0 only if all pass:

1. **Deliverables** — every submission-blocking file from BUILD_PLAN exists
   (README/LICENSE/DEMO/docs, scripts, edge wiring, infra/fc, seeds).
2. **Honesty gates** — README embeds the hero SVG on line 1, MIT is visible, the
   Status section is present, and the test-count claim in the README matches the
   number pytest actually collects.
3. **Functional smoke** — a short offline replay produces a door-ajar alarm and a
   gap-free, signature-verified hash chain; the seed curves are byte-stable.

Offline, keyless. No network, no hardware, no DASHSCOPE_API_KEY.

    python scripts/check_submission_readiness.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

Check = tuple[str, bool, str]


def _exists(rel: str) -> Check:
    p = ROOT / rel
    return (f"exists: {rel}", p.exists(), "" if p.exists() else "MISSING")


def _deliverables() -> list[Check]:
    required = [
        "README.md", "LICENSE", "pyproject.toml", "DEMO.md",
        "docs/friction-log.md", "docs/BENCH.md",
        "scripts/bench.py", "scripts/verify_offline.py", "scripts/check_submission_readiness.py",
        "edge/wiring.md",
        "infra/fc/s.yaml", "infra/fc/PROOF.md", "infra/fc/handler.py",
        "seeds/seed.py", "seeds/fridge.json",
        "seeds/door_ajar.csv", "seeds/defrost_cycle.csv",
        "seeds/compressor_drift.csv", "seeds/power_loss.csv",
        "src/permafrost/cloud/diagnose.py", "src/permafrost/cloud/distill.py",
        "src/permafrost/cloud/report.py",
    ]
    return [_exists(r) for r in required]


def _honesty_gates() -> list[Check]:
    out: list[Check] = []
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    first_line = readme.splitlines()[0] if readme else ""
    out.append((
        "README opens with icon + hero SVG embeds",
        first_line.strip().startswith('<div align="center">')
        and "docs/icon.svg" in readme[:800]
        and "docs/readme-hero.svg" in readme[:800],
        first_line[:70],
    ))
    out.append(("MIT license visible in README", "MIT" in readme, ""))
    out.append(("README has a Status / Pending section", bool(re.search(r"##+[^\n]*Status", readme)), ""))

    # test-count claim must match reality
    collected = _collect_test_count()
    claim = _readme_test_claim(readme)
    out.append((
        f"README test-count claim ({claim}) matches collected ({collected})",
        claim is not None and claim == collected,
        f"claim={claim} collected={collected}",
    ))
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8")
    out.append(("LICENSE is MIT", "MIT License" in lic or "Permission is hereby granted" in lic, ""))
    return out


def _readme_test_claim(readme: str) -> int | None:
    m = re.search(r"(\d{2,4})\s+(?:passing\s+)?(?:pytest\s+)?tests?", readme, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _collect_test_count() -> int | None:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
    except Exception:
        return None
    out = proc.stdout + proc.stderr
    # (a) some pytest configs print a summary line
    m = re.search(r"(\d+)\s+tests?\s+collected", out)
    if m:
        return int(m.group(1))
    # (b) `-q --collect-only` prints per-file "tests/test_x.py: N" — sum them
    per_file = [int(x) for x in re.findall(r"^\S+\.py:\s*(\d+)\s*$", out, re.MULTILINE)]
    if per_file:
        return sum(per_file)
    # (c) verbose node ids "path::test_name"
    ids = [ln for ln in out.splitlines() if "::" in ln]
    return len(ids) or None


def _functional_smoke() -> list[Check]:
    from permafrost.qwen.fake import FakeQwen
    from permafrost.qwen.transport import ALLOWED_MODELS
    from permafrost.replay import run_replay

    out: list[Check] = []
    db = Path(tempfile.mkdtemp()) / "readiness.db"
    result = run_replay(ROOT / "seeds" / "door_ajar.csv", db, transport=FakeQwen())
    out.append(("offline replay fired a local alarm", len(result.alarms) >= 1, f"alarms={len(result.alarms)}"))
    out.append(("produced a door_ajar verdict", any(e["verdict"]["cause"] == "door_ajar" for e in result.verdicts), ""))
    out.append((
        "hash chain verifies gap-free + signed roots",
        bool(result.chain_report and result.chain_report.ok and result.chain_report.roots_ok),
        result.chain_report.summary() if result.chain_report else "no report",
    ))
    out.append((
        "only verified Qwen model ids are allowed",
        ALLOWED_MODELS == frozenset({"qwen3.7-plus", "qwen3.6-flash", "text-embedding-v4", "qwen3-tts-instruct-flash"}),
        ", ".join(sorted(ALLOWED_MODELS)),
    ))

    seed_check = subprocess.run(
        [sys.executable, str(ROOT / "seeds" / "seed.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True,
    )
    out.append(("seed curves are byte-stable (seed.py --check)", seed_check.returncode == 0, ""))
    return out


def main() -> int:
    sections = [
        ("Deliverables", _deliverables()),
        ("Honesty gates", _honesty_gates()),
        ("Functional smoke", _functional_smoke()),
    ]
    all_ok = True
    print("Permafrost — submission readiness\n" + "=" * 42)
    for title, checks in sections:
        print(f"\n{title}")
        for label, ok, detail in checks:
            all_ok = all_ok and ok
            tail = f"  ({detail})" if detail and not ok else ""
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}{tail}")
    print("\n" + "=" * 42)
    print("READY TO SUBMIT" if all_ok else "NOT READY — fix the FAIL lines above")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
