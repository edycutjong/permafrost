"""Offline-first: event queue grows with the network down, syncs on reconnect, chain stays whole."""

from __future__ import annotations

import json

from helpers import sql, tiny_door_csv
from permafrost.cloud.app import create_app
from permafrost.crypto import dev_keys, unseal
from permafrost.daemon import EdgeDaemon
from permafrost.link import DiagnoserClient, LocalAppLink, OfflineError
from permafrost.qwen.fake import FakeQwen
from permafrost.sampler import CsvSource
from permafrost.storage import EdgeStore


def _wire(store, online=True):
    app = create_app(FakeQwen())
    link = LocalAppLink(app, online=online)
    diag = DiagnoserClient(link, dev_keys().sealing_public)
    return EdgeDaemon(store, diagnoser=diag), link


# --------------------------------------------------------------------------- the session offline replay

def test_offline_replay_alarm_still_fires(offline_door_replay):
    result, db, _ = offline_door_replay
    assert len(result.alarms) >= 1  # local reflex still protects with the cable pulled
    assert result.offline_ticks > 0


def test_offline_replay_queue_grew_then_drained(offline_door_replay):
    result, db, _ = offline_door_replay
    assert result.pending_peak >= 1  # events queued while offline
    assert result.pending_after == 0  # everything synced after reconnect
    assert result.synced_total >= 1


def test_offline_replay_chain_verifies_after_reconnect(offline_door_replay):
    result, db, _ = offline_door_replay
    assert result.chain_report.ok and result.chain_report.roots_ok


def test_offline_replay_link_state_transitions_logged(offline_door_replay):
    result, db, _ = offline_door_replay
    kinds = [json.loads(e)["kind"] for (e,) in sql(db, "SELECT entry FROM log_chain ORDER BY seq")]
    assert kinds.count("link_state") >= 2  # at least one down + one back up


def test_offline_replay_produces_door_verdicts_on_sync(offline_door_replay):
    result, db, _ = offline_door_replay
    assert any(e["verdict"]["cause"] == "door_ajar" for e in result.verdicts)


# --------------------------------------------------------------------------- ECIES envelope

def test_queued_batches_are_ecies_sealed(tmp_path):
    store = EdgeStore(tmp_path / "off.db")
    d, link = _wire(store, online=False)  # start offline so nothing flushes
    src = CsvSource(tiny_door_csv(tmp_path / "door.csv"))
    while (r := src.read()) is not None:
        d.process_tick(r)
    rows = sql(tmp_path / "off.db", "SELECT sealed FROM queue")
    assert rows, "an escalation should have been queued"
    sealed = bytes(rows[0][0])
    assert not sealed.startswith(b"unsent:")  # a real ECIES box, not the no-diagnoser stub
    payload = json.loads(unseal(sealed, dev_keys().sealing_private))
    assert "curve" in payload and "fridge_meta" in payload
    store.close()


def test_diagnoser_seal_is_ciphertext_and_roundtrips():
    diag = DiagnoserClient(LocalAppLink(create_app(FakeQwen())), dev_keys().sealing_public)
    payload = {"curve": [{"ts": 0, "temp_c": 4}], "fridge_meta": {"id": "x"}}
    sealed = diag.seal_payload(payload)
    assert json.dumps(payload).encode() not in sealed  # not plaintext on the wire
    assert json.loads(unseal(sealed, dev_keys().sealing_private)) == payload


# --------------------------------------------------------------------------- flush semantics

def test_flush_queue_stops_when_offline(tmp_path):
    store = EdgeStore(tmp_path / "off.db")
    d, link = _wire(store, online=False)
    src = CsvSource(tiny_door_csv(tmp_path / "door.csv"))
    last = None
    while (r := src.read()) is not None:
        d.process_tick(r)
        last = r.ts
    pending = store.pending_count()
    assert pending >= 1
    # link is down -> flush delivers nothing and leaves the queue intact
    assert d.flush_queue(last) == []
    assert store.pending_count() == pending
    store.close()


def test_reconnect_flushes_entire_queue(tmp_path):
    store = EdgeStore(tmp_path / "off.db")
    d, link = _wire(store, online=False)
    src = CsvSource(tiny_door_csv(tmp_path / "door.csv"))
    last = None
    while (r := src.read()) is not None:
        d.process_tick(r)
        last = r.ts
    assert store.pending_count() >= 1
    link.set_online(True)
    delivered = d.flush_queue(last)
    assert len(delivered) >= 1 and store.pending_count() == 0
    assert all("verdict" in env for env in delivered)
    store.close()


def test_link_raises_offline_error_when_down():
    link = LocalAppLink(create_app(FakeQwen()), online=False)
    try:
        link.diagnose_sealed(b"anything")
        assert False, "expected OfflineError"
    except OfflineError:
        pass
