# Contributing

Thanks for your interest in improving Permafrost!

## Getting Started
1. Fork the repo and branch from `main`: `git checkout -b feat/your-feature`
2. Create a Python 3.12 virtualenv and install dev deps:
   ```bash
   python3.12 -m venv .venv && source .venv/bin/activate
   pip install -e ".[dev]"
   ```
3. Run the zero-hardware replay to sanity-check the loop:
   ```bash
   permafrost replay --curve seeds/door_ajar.csv --db audit.db --fresh
   ```

## Before You Open a PR
- `ruff check .` passes (lint).
- `mypy .` passes (type check).
- `pytest --cov=permafrost` passes — all 326+ tests, offline, no API keys.
- `python scripts/verify_offline.py` passes (offline-degradation invariant).
- `python scripts/check_submission_readiness.py` passes (deliverables + honesty gates).
- Add or update tests for any behavior change, especially the I1-I4 invariants in
  `tests/test_invariants.py`.
- Keep commits conventional (`feat:`, `fix:`, `docs:`, `chore:`).

## Reporting Bugs / Requesting Features
Open an issue using the provided templates. Include repro steps, expected vs.
actual behavior, and environment details (Python version, OS, hardware vs. replay).
