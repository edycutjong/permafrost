"""EdgeStore — crash-safe SQLite (WAL): readings ring, events, queue, rules, meta."""

from __future__ import annotations

import pytest

from helpers import R
from permafrost.storage import RING_WINDOW_S, EdgeStore, Reading
from permafrost.timeutil import VIRTUAL_EPOCH_TS as T0, day_of


@pytest.fixture()
def store(tmp_path):
    s = EdgeStore(tmp_path / "s.db")
    yield s
    s.close()


def test_opens_in_wal_mode(store):
    assert store.journal_mode().lower() == "wal"


def test_schema_tables_present(store):
    names = {
        r[0]
        for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"readings", "events", "log_chain", "rules", "queue", "roots", "daily_stats", "meta"} <= names


def test_add_reading_and_count(store):
    for i in range(5):
        store.add_reading(R(i * 10))
    store.commit()
    assert store.reading_count() == 5
    assert store.last_reading_ts() == T0 + 40


def test_last_reading_ts_empty_is_none(store):
    assert store.last_reading_ts() is None


def test_readings_since_filters_and_orders(store):
    for i in range(10):
        store.add_reading(R(i * 10, temp=4.0 + i))
    store.commit()
    got = store.readings_since(T0 + 50)
    assert [r.temp_c for r in got] == [9.0, 10.0, 11.0, 12.0, 13.0]
    assert all(isinstance(r, Reading) for r in got)


def test_evict_ring_drops_old(store):
    store.add_reading(R(0))
    newest = R(RING_WINDOW_S + 100)
    store.add_reading(newest)
    store.commit()
    evicted = store.evict_ring(newest.ts)  # absolute now; cutoff = now - 24h
    store.commit()
    assert evicted == 1 and store.reading_count() == 1


def test_daily_means_group_by_day(store):
    # two days: day1 mean 4.0, day2 mean 6.0
    store.add_reading(R(0, temp=4.0))
    store.add_reading(R(3600, temp=4.0))
    store.add_reading(R(86400, temp=6.0))
    store.commit()
    means = dict(store.daily_means())
    assert means[day_of(T0)] == 4.0
    assert means[day_of(T0 + 86400)] == 6.0


def test_events_since_and_kind_filter(store):
    store.add_event(T0, "door", {"door_open": True})
    store.add_event(T0 + 10, "power", {"power_ok": False})
    store.add_event(T0 + 20, "door", {"door_open": False})
    store.commit()
    doors = store.events_since(T0, kinds=["door"])
    assert len(doors) == 2 and all(e["kind"] == "door" for e in doors)
    allev = store.events_since(T0)
    assert len(allev) == 3 and allev[0]["ts"] == T0


def test_queue_enqueue_pending_sync(store):
    qid = store.enqueue(T0, "digest-1", b"sealed-bytes")
    store.commit()
    assert store.pending_count() == 1 and store.synced_count() == 0
    pend = store.pending_batches()
    assert pend == [(qid, "digest-1", b"sealed-bytes")]
    store.mark_synced(qid, T0 + 5)
    store.commit()
    assert store.pending_count() == 0 and store.synced_count() == 1


def test_rules_store_and_active_returns_highest(store):
    store.store_rules(1, '{"v":1}', "sig1", "builtin", T0)
    store.store_rules(2, '{"v":2}', "sig2", "distilled", T0 + 1)
    store.commit()
    ver, js, sig = store.active_rules()
    assert ver == 2 and sig == "sig2"
    assert store.rules_history() == [(1, "builtin"), (2, "distilled")]


def test_active_rules_none_when_empty(store):
    assert store.active_rules() is None


def test_meta_get_set_default(store):
    assert store.meta_get("missing", "fallback") == "fallback"
    store.meta_set("k", "v")
    store.commit()
    assert store.meta_get("k") == "v"
    store.meta_set("k", "v2")
    assert store.meta_get("k") == "v2"


def test_daily_stats_min_max_tracked(store):
    store.add_reading(R(0, temp=3.0))
    store.add_reading(R(10, temp=7.0))
    store.add_reading(R(20, temp=5.0))
    store.commit()
    row = store.conn.execute("SELECT n, min_temp, max_temp FROM daily_stats").fetchone()
    assert row == (3, 3.0, 7.0)
