"""Cloud FastAPI brain: /healthz, /diagnose (plain + sealed), /distill, /report/weekly."""

from __future__ import annotations

import base64

import pytest

from permafrost.canonical import canonical_json
from permafrost.cloud.app import create_app, create_default_app
from permafrost.crypto import dev_keys, seal
from permafrost.link import make_inprocess_client
from permafrost.qwen.fake import FakeQwen
from permafrost.qwen.transport import ChatResult, UsageMeter
from permafrost.rules import RuleBundle


@pytest.fixture()
def client():
    c = make_inprocess_client(create_app(FakeQwen()))
    yield c
    c.close()


class _BadTransport:
    """Always returns non-JSON ``chat`` content, so /diagnose and /distill both
    hit their upstream-failure (502) branches — FakeQwen's real fixtures never fail."""

    def __init__(self):
        self.usage = UsageMeter()

    def chat(self, model, messages, **kw):
        return ChatResult(content="not json", task_id="bad", model=model, prompt_tokens=1, completion_tokens=1)

    def embed(self, model, texts):
        return [[0.0] * 8 for _ in texts]

    def tts(self, model, text, instructions):
        return b""


def _door_payload():
    return {
        "fridge_meta": {"fridge_id": "x"},
        "curve": [
            {"ts": 0, "temp_c": 4.0, "humidity_pct": 45.0, "door_open": True, "power_ok": True},
            {"ts": 1200, "temp_c": 7.4, "humidity_pct": 78.0, "door_open": True, "power_ok": True},
        ],
        "daily_means": [],
        "trigger": {},
    }


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_diagnose_plaintext(client):
    r = client.post("/diagnose", json=_door_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"]["cause"] == "door_ajar"
    assert body["task_id"].startswith("fake-")
    assert body["verdict"]["guidance_citation"].strip()


def test_diagnose_sealed(client):
    sealed = seal(canonical_json(_door_payload()), dev_keys().sealing_public)
    r = client.post("/diagnose", json={"sealed_b64": base64.b64encode(sealed).decode()})
    assert r.status_code == 200 and r.json()["verdict"]["cause"] == "door_ajar"


def test_diagnose_bad_seal_rejected(client):
    r = client.post("/diagnose", json={"sealed_b64": base64.b64encode(b"not a sealed box").decode()})
    assert r.status_code == 400


def test_diagnose_single_point_curve_422(client):
    r = client.post("/diagnose", json={"curve": [{"ts": 0, "temp_c": 4}]})
    assert r.status_code == 422


def test_healthz_counts_history(client):
    client.post("/diagnose", json=_door_payload())
    assert client.get("/healthz").json()["verdicts_seen"] == 1


def test_distill_returns_signed_bundle(client):
    client.post("/diagnose", json=_door_payload())  # seed some history
    r = client.post("/distill", json={"current_version": 1, "now_ts": 0.0})
    assert r.status_code == 200
    body = r.json()
    bundle = RuleBundle.parse(body["bundle"])
    assert bundle.version == 2
    assert bundle.verify(body["sig"], body["verify_key"])


def test_distill_uses_server_history_when_none_given(client):
    for _ in range(3):
        client.post("/diagnose", json=_door_payload())
    r = client.post("/distill", json={"current_version": 1})
    assert r.status_code == 200
    # door history tightens the door timer
    door = next(x for x in r.json()["bundle"]["rules"] if x["id"] == "door_timer")
    assert door["max_open_s"] == 90


def test_report_weekly(client):
    client.post("/diagnose", json=_door_payload())
    r = client.get("/report/weekly", params={"week": 2})
    assert r.status_code == 200
    assert "compliance report" in r.json()["markdown"]


def test_report_weekly_rejects_invalid_week(client):
    assert client.get("/report/weekly", params={"week": 99}).status_code == 422


def test_diagnose_history_accumulates(client):
    client.post("/diagnose", json=_door_payload())
    client.post("/diagnose", json=_door_payload())
    assert client.get("/healthz").json()["verdicts_seen"] == 2


# --------------------------------------------------------------------------- upstream failures

def test_diagnose_returns_502_when_the_model_misbehaves():
    c = make_inprocess_client(create_app(_BadTransport()))
    try:
        r = c.post("/diagnose", json=_door_payload())
        assert r.status_code == 502
    finally:
        c.close()


def test_distill_rejects_extra_fields_with_422(client):
    r = client.post("/distill", json={"current_version": 1, "surprise_field": True})
    assert r.status_code == 422


def test_distill_returns_502_when_the_model_misbehaves():
    c = make_inprocess_client(create_app(_BadTransport()))
    try:
        r = c.post("/distill", json={"verdicts": [{"cause": "door_ajar"}], "current_version": 1})
        assert r.status_code == 502
    finally:
        c.close()


def test_create_default_app_boots_healthy(monkeypatch):
    # no live env set -> default_transport() falls back to FakeQwen, still fully offline
    monkeypatch.delenv("PERMAFROST_LIVE", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    c = make_inprocess_client(create_default_app())
    try:
        r = c.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"] is True
    finally:
        c.close()
