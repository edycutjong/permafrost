"""Hash-chain log + daily Merkle roots (COMPLEXITY §2).

Chain construction
------------------
Entry ``i`` (1-based) is a dict ``{"seq", "ts", "kind", "payload"}`` stored as
its canonical-JSON string, and::

    h_i = SHA256( raw(h_{i-1}) || canonical_json(entry_i) )

where ``raw(h)`` is the 32-byte digest (hex-decoded) and
``h_0 = SHA256(b"permafrost:genesis:v1")``. Gaps in *time* are provable
absences; gaps in *sequence* are tamper.

Daily Merkle roots
------------------
Leaves are the day's chain hashes in seq order; parents are
``SHA256(left || right)`` (odd leaf duplicated). The root record
``{"day", "root", "first_seq", "last_seq", "n"}`` is canonicalised and
Ed25519-signed — archive target: OSS (see infra/fc/PROOF.md).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import canonical_dumps, canonical_json
from .crypto import sign, verify_signature
from .storage import EdgeStore
from .timeutil import day_of

__all__ = [
    "GENESIS_HASH",
    "ChainLogger",
    "ChainReport",
    "verify_chain",
    "merkle_root",
    "sign_daily_roots",
]

GENESIS_HASH = hashlib.sha256(b"permafrost:genesis:v1").hexdigest()


def _link(prev_hash_hex: str, entry_canonical: bytes) -> str:
    return hashlib.sha256(bytes.fromhex(prev_hash_hex) + entry_canonical).hexdigest()


class ChainLogger:
    """Appends entries to the tamper-evident chain inside an EdgeStore."""

    def __init__(self, store: EdgeStore):
        self.store = store
        row = store.conn.execute(
            "SELECT seq, hash FROM log_chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        self._seq: int
        self._head: str
        self._seq, self._head = (row[0], row[1]) if row else (0, GENESIS_HASH)

    @property
    def head(self) -> str:
        return self._head

    @property
    def seq(self) -> int:
        return self._seq

    def append(self, ts: float, kind: str, payload: dict[str, Any]) -> tuple[int, str]:
        """Append one entry; returns ``(seq, hash)``. Caller commits."""
        seq = self._seq + 1
        entry = {"seq": seq, "ts": ts, "kind": kind, "payload": payload}
        entry_str = canonical_dumps(entry)
        h = _link(self._head, entry_str.encode("utf-8"))
        self.store.conn.execute(
            "INSERT INTO log_chain(seq, ts, entry, hash) VALUES(?,?,?,?)",
            (seq, ts, entry_str, h),
        )
        self._seq, self._head = seq, h
        return seq, h


# --------------------------------------------------------------------------- verification


@dataclass
class ChainReport:
    ok: bool
    entries: int
    first_bad_seq: int | None = None
    reason: str | None = None
    roots_checked: int = 0
    roots_ok: bool = True
    root_failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok and self.roots_ok:
            return (
                f"OK — {self.entries} entries re-derived, chain intact; "
                f"{self.roots_checked} signed daily root(s) verified"
            )
        parts = []
        if not self.ok:
            parts.append(f"CHAIN FAIL at seq {self.first_bad_seq}: {self.reason}")
        if not self.roots_ok:
            parts.append(f"ROOT FAIL: {', '.join(self.root_failures)}")
        return " | ".join(parts)


def verify_chain(db_path: str | Path, verify_key_hex: str | None = None) -> ChainReport:
    """Re-derive every hash from genesis; then re-derive + signature-check daily roots.

    A missing/duplicated seq, an edited entry byte, or an edited stored hash all
    fail (invariant I2). Time gaps do NOT fail — they are provable absences (I1).
    """
    store = EdgeStore(db_path)
    try:
        rows = store.conn.execute("SELECT seq, ts, entry, hash FROM log_chain ORDER BY seq").fetchall()
        head = GENESIS_HASH
        expected_seq = 1
        day_hashes: dict[str, list[str]] = {}
        for seq, ts, entry_str, stored_hash in rows:
            if seq != expected_seq:
                return ChainReport(False, len(rows), first_bad_seq=seq, reason=f"sequence gap (expected {expected_seq})")
            try:
                entry = json.loads(entry_str)
            except json.JSONDecodeError:
                return ChainReport(False, len(rows), first_bad_seq=seq, reason="entry is not valid JSON")
            if entry.get("seq") != seq:
                return ChainReport(False, len(rows), first_bad_seq=seq, reason="embedded seq mismatch")
            if canonical_dumps(entry) != entry_str:
                return ChainReport(False, len(rows), first_bad_seq=seq, reason="entry not canonical JSON")
            h = _link(head, entry_str.encode("utf-8"))
            if h != stored_hash:
                return ChainReport(False, len(rows), first_bad_seq=seq, reason="hash mismatch (tamper)")
            head = h
            expected_seq += 1
            day_hashes.setdefault(day_of(ts), []).append(h)

        report = ChainReport(True, len(rows))

        root_rows = store.conn.execute(
            "SELECT day, merkle_root, sig, first_seq, last_seq, n FROM roots ORDER BY day"
        ).fetchall()
        for day, root_hex, sig_hex, first_seq, last_seq, n in root_rows:
            report.roots_checked += 1
            hashes = day_hashes.get(day, [])
            derived = merkle_root(hashes)
            if derived != root_hex or len(hashes) != n:
                report.roots_ok = False
                report.root_failures.append(f"{day}: recomputed root differs")
                continue
            if verify_key_hex is not None:
                record = _root_record(day, root_hex, first_seq, last_seq, n)
                if not verify_signature(canonical_json(record), sig_hex, verify_key_hex):
                    report.roots_ok = False
                    report.root_failures.append(f"{day}: Ed25519 signature invalid")
        return report
    finally:
        store.close()


# --------------------------------------------------------------------------- merkle


def merkle_root(hashes_hex: list[str]) -> str:
    """Merkle root (hex) over chain hashes; empty day -> hash of empty string."""
    if not hashes_hex:
        return hashlib.sha256(b"").hexdigest()
    level = [bytes.fromhex(h) for h in hashes_hex]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [hashlib.sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)]
    return level[0].hex()


def _root_record(day: str, root_hex: str, first_seq: int, last_seq: int, n: int) -> dict[str, Any]:
    return {"day": day, "root": root_hex, "first_seq": first_seq, "last_seq": last_seq, "n": n}


def sign_daily_roots(store: EdgeStore, signing_seed_hex: str, *, include_partial_day: str | None = None) -> list[str]:
    """Compute + Ed25519-sign a Merkle root for every day present in the chain.

    Days already signed are skipped (idempotent) unless they were signed as
    partial and now have more entries. Returns the list of days (re)signed.
    """
    rows = store.conn.execute("SELECT seq, ts, hash FROM log_chain ORDER BY seq").fetchall()
    by_day: dict[str, list[tuple[int, str]]] = {}
    for seq, ts, h in rows:
        by_day.setdefault(day_of(ts), []).append((seq, h))

    signed: list[str] = []
    for day, pairs in sorted(by_day.items()):
        first_seq, last_seq, n = pairs[0][0], pairs[-1][0], len(pairs)
        existing = store.conn.execute("SELECT n, partial FROM roots WHERE day=?", (day,)).fetchone()
        if existing and existing[0] == n:
            continue
        root = merkle_root([h for _, h in pairs])
        record = _root_record(day, root, first_seq, last_seq, n)
        sig = sign(canonical_json(record), signing_seed_hex)
        partial = 1 if (include_partial_day is not None and day == include_partial_day) else 0
        store.conn.execute(
            "INSERT INTO roots(day, merkle_root, sig, first_seq, last_seq, n, partial) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET merkle_root=excluded.merkle_root, sig=excluded.sig, "
            "first_seq=excluded.first_seq, last_seq=excluded.last_seq, n=excluded.n, partial=excluded.partial",
            (day, root, sig, first_seq, last_seq, n, partial),
        )
        signed.append(day)
    store.commit()
    return signed
