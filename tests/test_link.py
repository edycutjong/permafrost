"""CloudLink surfaces not exercised elsewhere: HttpLink (the real deployed-endpoint
link, monkeypatched here so it never touches a socket) and DiagnoserClient.diagnose()
(the payload-in-one-call helper; the replay/daemon path always uses diagnose_sealed()
directly, so this call site is otherwise dead in the suite).

LocalAppLink + DiagnoserClient.diagnose_sealed() are already covered thoroughly by
test_offline.py and test_app.py; not duplicated here.
"""

from __future__ import annotations

import httpx
import pytest

from permafrost.cloud.app import create_app
from permafrost.crypto import dev_keys
from permafrost.link import DiagnoserClient, HttpLink, LocalAppLink, OfflineError
from permafrost.qwen.fake import FakeQwen


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


# --------------------------------------------------------------------------- DiagnoserClient.diagnose()

def test_diagnoser_diagnose_seals_calls_and_returns_verdict():
    diag = DiagnoserClient(LocalAppLink(create_app(FakeQwen())), dev_keys().sealing_public)
    envelope = diag.diagnose(_door_payload())
    assert envelope["verdict"]["cause"] == "door_ajar"
    assert envelope["task_id"].startswith("fake-")


# --------------------------------------------------------------------------- HttpLink (monkeypatched httpx)

def test_http_link_is_online_true_on_200(monkeypatch):
    link = HttpLink("https://cloud.example.invalid")

    def _fake_get(path, **kw):
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", "https://cloud.example.invalid" + path))

    monkeypatch.setattr(link._client, "get", _fake_get)
    assert link.is_online() is True


def test_http_link_is_online_false_on_connection_error(monkeypatch):
    link = HttpLink("https://cloud.example.invalid")

    def _fake_get(path, **kw):
        raise httpx.ConnectError("no route", request=httpx.Request("GET", "https://cloud.example.invalid" + path))

    monkeypatch.setattr(link._client, "get", _fake_get)
    assert link.is_online() is False


def test_http_link_diagnose_sealed_success(monkeypatch):
    link = HttpLink("https://cloud.example.invalid")

    def _fake_post(path, **kw):
        req = httpx.Request("POST", "https://cloud.example.invalid" + path)
        return httpx.Response(200, json={"verdict": {"cause": "door_ajar"}, "task_id": "t1"}, request=req)

    monkeypatch.setattr(link._client, "post", _fake_post)
    result = link.diagnose_sealed(b"sealed-bytes")
    assert result["task_id"] == "t1"


def test_http_link_diagnose_sealed_raises_offline_on_connection_error(monkeypatch):
    link = HttpLink("https://cloud.example.invalid")

    def _fake_post(path, **kw):
        raise httpx.ConnectError("no route", request=httpx.Request("POST", "https://cloud.example.invalid" + path))

    monkeypatch.setattr(link._client, "post", _fake_post)
    with pytest.raises(OfflineError):
        link.diagnose_sealed(b"sealed-bytes")


def test_http_link_diagnose_sealed_raises_offline_on_http_error_status(monkeypatch):
    link = HttpLink("https://cloud.example.invalid")

    def _fake_post(path, **kw):
        req = httpx.Request("POST", "https://cloud.example.invalid" + path)
        return httpx.Response(500, request=req)

    monkeypatch.setattr(link._client, "post", _fake_post)
    with pytest.raises(OfflineError):
        link.diagnose_sealed(b"sealed-bytes")
