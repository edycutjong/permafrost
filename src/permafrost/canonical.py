"""Canonical JSON — the single byte-representation everything cryptographic hashes/signs.

Rules (stable forever, documented in docs/SPEC-COLDCHAIN.md):
- keys sorted lexicographically at every level
- separators ``(",", ":")`` — no whitespace
- ``ensure_ascii=False`` and UTF-8 encoding
- floats use Python ``repr`` semantics (shortest round-trip); producers are
  expected to round to sane precision before logging.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["canonical_json", "canonical_dumps"]


def canonical_dumps(obj: Any) -> str:
    """Canonical JSON as a ``str``."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_json(obj: Any) -> bytes:
    """Canonical JSON as UTF-8 ``bytes`` — the hash/sign input."""
    return canonical_dumps(obj).encode("utf-8")
