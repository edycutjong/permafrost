"""Hash-chain construction, verification, and Merkle roots."""

import hashlib
import json

import pytest

from helpers import flip_one_byte, sql
from permafrost.canonical import canonical_json
from permafrost.chain import ChainReport, GENESIS_HASH, ChainLogger, merkle_root, sign_daily_roots, verify_chain
from permafrost.crypto import dev_keys
from permafrost.storage import EdgeStore
from permafrost.timeutil import VIRTUAL_EPOCH_TS


@pytest.fixture()
def store(tmp_path):
    s = EdgeStore(tmp_path / "chain.db")
    yield s, tmp_path / "chain.db"
    s.close()


def _fill(chain: ChainLogger, store: EdgeStore, n: int = 10) -> None:
    for i in range(n):
        chain.append(VIRTUAL_EPOCH_TS + i * 10, "reading", {"temp_c": 4.0 + i * 0.01, "i": i})
    store.commit()


def test_genesis_constant():
    assert GENESIS_HASH == hashlib.sha256(b"permafrost:genesis:v1").hexdigest()


def test_append_increments_seq(store):
    s, _ = store
    chain = ChainLogger(s)
    assert chain.seq == 0 and chain.head == GENESIS_HASH
    seq1, h1 = chain.append(VIRTUAL_EPOCH_TS, "reading", {"temp_c": 4.0})
    seq2, h2 = chain.append(VIRTUAL_EPOCH_TS + 10, "reading", {"temp_c": 4.1})
    assert (seq1, seq2) == (1, 2) and h1 != h2 and chain.head == h2


def test_hash_formula_manual_recompute(store):
    """h_i = SHA256(raw(h_{i-1}) || canonical_json(entry_i)) — recomputed by hand."""
    s, path = store
    chain = ChainLogger(s)
    _, h1 = chain.append(VIRTUAL_EPOCH_TS, "reading", {"temp_c": 4.0})
    s.commit()
    entry = {"seq": 1, "ts": VIRTUAL_EPOCH_TS, "kind": "reading", "payload": {"temp_c": 4.0}}
    manual = hashlib.sha256(bytes.fromhex(GENESIS_HASH) + canonical_json(entry)).hexdigest()
    assert h1 == manual


def test_verify_ok_and_counts(store):
    s, path = store
    chain = ChainLogger(s)
    _fill(chain, s, 25)
    report = verify_chain(path)
    assert report.ok and report.entries == 25


def test_verify_empty_chain_ok(store):
    _, path = store
    report = verify_chain(path)
    assert report.ok and report.entries == 0


def test_time_gaps_are_not_failures(store):
    """Provable absence: a 40-min hole in ts must NOT fail verification."""
    s, path = store
    chain = ChainLogger(s)
    chain.append(VIRTUAL_EPOCH_TS, "reading", {"temp_c": 4.0})
    chain.append(VIRTUAL_EPOCH_TS + 2400.0, "gap", {"gap_s": 2400.0})
    s.commit()
    assert verify_chain(path).ok


def test_tampered_entry_byte_fails(store):
    s, path = store
    chain = ChainLogger(s)
    _fill(chain, s, 12)
    (entry_str,) = sql(path, "SELECT entry FROM log_chain WHERE seq=6")[0]
    sql(path, "UPDATE log_chain SET entry=? WHERE seq=6", (flip_one_byte(entry_str),))
    report = verify_chain(path)
    assert not report.ok and report.first_bad_seq == 6


def test_tampered_stored_hash_fails(store):
    s, path = store
    chain = ChainLogger(s)
    _fill(chain, s, 12)
    (h,) = sql(path, "SELECT hash FROM log_chain WHERE seq=4")[0]
    sql(path, "UPDATE log_chain SET hash=? WHERE seq=4", (flip_one_byte(h),))
    assert not verify_chain(path).ok


def test_deleted_row_is_sequence_gap(store):
    s, path = store
    chain = ChainLogger(s)
    _fill(chain, s, 12)
    sql(path, "DELETE FROM log_chain WHERE seq=7")
    report = verify_chain(path)
    assert not report.ok and "sequence gap" in (report.reason or "")


def test_swapped_entries_fail(store):
    s, path = store
    chain = ChainLogger(s)
    _fill(chain, s, 12)
    a = sql(path, "SELECT entry, hash FROM log_chain WHERE seq=3")[0]
    b = sql(path, "SELECT entry, hash FROM log_chain WHERE seq=4")[0]
    sql(path, "UPDATE log_chain SET entry=?, hash=? WHERE seq=3", b)
    sql(path, "UPDATE log_chain SET entry=?, hash=? WHERE seq=4", a)
    assert not verify_chain(path).ok


def test_non_canonical_entry_detected(store):
    s, path = store
    chain = ChainLogger(s)
    _fill(chain, s, 3)
    (entry_str,) = sql(path, "SELECT entry FROM log_chain WHERE seq=2")[0]
    padded = json.dumps(json.loads(entry_str), indent=1)  # same content, different bytes
    sql(path, "UPDATE log_chain SET entry=? WHERE seq=2", (padded,))
    assert not verify_chain(path).ok


def test_chain_continues_across_reopen(store):
    s, path = store
    chain = ChainLogger(s)
    _fill(chain, s, 5)
    head_before = chain.head
    s.close()
    s2 = EdgeStore(path)
    chain2 = ChainLogger(s2)
    assert chain2.seq == 5 and chain2.head == head_before
    chain2.append(VIRTUAL_EPOCH_TS + 999, "reading", {"temp_c": 5.0})
    s2.commit()
    s2.close()
    report = verify_chain(path)
    assert report.ok and report.entries == 6
    # keep the outer fixture close() happy
    store[0].conn = EdgeStore(path).conn


def test_merkle_root_deterministic_and_odd_leaves():
    h1 = hashlib.sha256(b"a").hexdigest()
    h2 = hashlib.sha256(b"b").hexdigest()
    h3 = hashlib.sha256(b"c").hexdigest()
    assert merkle_root([h1, h2, h3]) == merkle_root([h1, h2, h3])
    assert merkle_root([h1, h2, h3]) != merkle_root([h1, h2])
    assert merkle_root([h1]) == h1  # single leaf is its own root
    assert merkle_root([]) == hashlib.sha256(b"").hexdigest()


def test_merkle_order_matters():
    h1 = hashlib.sha256(b"a").hexdigest()
    h2 = hashlib.sha256(b"b").hexdigest()
    assert merkle_root([h1, h2]) != merkle_root([h2, h1])


def test_sign_daily_roots_and_verify(store):
    s, path = store
    keys = dev_keys()
    chain = ChainLogger(s)
    _fill(chain, s, 20)
    signed = sign_daily_roots(s, keys.signing_seed)
    assert signed == ["2026-01-05"]
    report = verify_chain(path, keys.verify_key)
    assert report.ok and report.roots_ok and report.roots_checked == 1
    # idempotent: second call signs nothing new
    assert sign_daily_roots(s, keys.signing_seed) == []


def test_root_tamper_fails(store):
    s, path = store
    keys = dev_keys()
    chain = ChainLogger(s)
    _fill(chain, s, 8)
    sign_daily_roots(s, keys.signing_seed)
    (root,) = sql(path, "SELECT merkle_root FROM roots")[0]
    sql(path, "UPDATE roots SET merkle_root=?", (flip_one_byte(root),))
    report = verify_chain(path, keys.verify_key)
    assert report.ok and not report.roots_ok  # chain fine, root record poisoned


def test_root_signature_wrong_key_fails(store):
    s, path = store
    keys = dev_keys()
    chain = ChainLogger(s)
    _fill(chain, s, 8)
    sign_daily_roots(s, keys.signing_seed)
    from permafrost.crypto import generate_signing_seed, verify_key_of

    report = verify_chain(path, verify_key_of(generate_signing_seed()))
    assert report.ok and not report.roots_ok


def test_summary_reports_both_chain_and_root_failures():
    report = ChainReport(
        ok=False, entries=9, first_bad_seq=4, reason="hash mismatch (tamper)",
        roots_checked=1, roots_ok=False, root_failures=["2026-01-05: recomputed root differs"],
    )
    text = report.summary()
    assert "CHAIN FAIL at seq 4: hash mismatch (tamper)" in text
    assert "ROOT FAIL: 2026-01-05: recomputed root differs" in text


def test_entry_that_is_not_valid_json_fails_verification(store):
    s, path = store
    chain = ChainLogger(s)
    _fill(chain, s, 6)
    sql(path, "UPDATE log_chain SET entry=? WHERE seq=3", ("not json at all {{{",))
    report = verify_chain(path)
    assert not report.ok
    assert report.first_bad_seq == 3
    assert "not valid JSON" in (report.reason or "")


def test_root_resigned_when_day_grows(store):
    s, path = store
    keys = dev_keys()
    chain = ChainLogger(s)
    _fill(chain, s, 5)
    assert sign_daily_roots(s, keys.signing_seed) == ["2026-01-05"]
    chain.append(VIRTUAL_EPOCH_TS + 500, "reading", {"temp_c": 4.2})
    s.commit()
    assert sign_daily_roots(s, keys.signing_seed) == ["2026-01-05"]
    assert verify_chain(path, keys.verify_key).roots_ok
