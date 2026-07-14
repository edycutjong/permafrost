"""LiveQwen: the DashScope-compatible transport, model allow-list, token accounting.

FULLY offline: ``openai.OpenAI`` is monkeypatched to a fake client that never opens a
socket, so these tests need neither DASHSCOPE_API_KEY nor PERMAFROST_LIVE. FakeQwen
(the default transport) is covered elsewhere — this file targets LiveQwen's own
plumbing, which every other test path skips entirely.
"""

from __future__ import annotations

import types

import pytest

from permafrost.qwen.transport import (
    MODEL_DIAGNOSIS,
    MODEL_DISTILL,
    MODEL_EMBEDDING,
    MODEL_TTS,
    LiveQwen,
    UsageMeter,
)


class _FakeChatResp:
    def __init__(self, content, usage=None, id_="task-1"):
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
        self.usage = usage
        self.id = id_


class _FakeEmbedResp:
    def __init__(self, n):
        self.data = [types.SimpleNamespace(embedding=[0.1, 0.2]) for _ in range(n)]


class _FakeAudioResp:
    def read(self):
        return b"AUDIO-BYTES"


class _FakeOpenAIClient:
    """Stands in for ``openai.OpenAI`` — same call shape, zero network."""

    def __init__(self, api_key, base_url):
        self.api_key = api_key
        self.base_url = base_url
        self.last_kwargs: dict = {}
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._chat_create))
        self.embeddings = types.SimpleNamespace(create=self._embed_create)
        self.audio = types.SimpleNamespace(speech=types.SimpleNamespace(create=self._tts_create))

    def _chat_create(self, **kwargs):
        self.last_kwargs = kwargs
        usage = types.SimpleNamespace(prompt_tokens=12, completion_tokens=6)
        return _FakeChatResp('{"ok": true}', usage=usage)

    def _embed_create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeEmbedResp(len(kwargs["input"]))

    def _tts_create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeAudioResp()


@pytest.fixture()
def fake_openai(monkeypatch):
    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAIClient)


def test_live_qwen_requires_a_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        LiveQwen()


def test_live_qwen_constructs_with_explicit_key(fake_openai):
    q = LiveQwen(api_key="test-key")
    assert isinstance(q.usage, UsageMeter)
    assert q.base_url.startswith("https://")


def test_live_qwen_constructs_from_env_key(monkeypatch, fake_openai):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "env-key")
    q = LiveQwen()
    assert q.usage.calls == []


def test_chat_rejects_unlisted_model(fake_openai):
    q = LiveQwen(api_key="k")
    with pytest.raises(ValueError, match="not in Permafrost's allowed set"):
        q.chat("gpt-4o", [{"role": "user", "content": "hi"}])


def test_chat_records_usage_and_returns_result(fake_openai):
    q = LiveQwen(api_key="k")
    result = q.chat(
        MODEL_DIAGNOSIS, [{"role": "user", "content": "diagnose"}],
        thinking=True, tools=[{"type": "function", "function": {"name": "sound_alarm"}}],
    )
    assert result.content == '{"ok": true}'
    assert result.model == MODEL_DIAGNOSIS and result.task_id == "task-1" and result.thinking is True
    assert result.prompt_tokens == 12 and result.completion_tokens == 6
    assert q.usage.totals()[MODEL_DIAGNOSIS] == {"calls": 1, "prompt_tokens": 12, "completion_tokens": 6}


def test_chat_json_response_false_and_no_tools_still_works(fake_openai):
    q = LiveQwen(api_key="k")
    result = q.chat(MODEL_DISTILL, [{"role": "user", "content": "x"}], json_response=False)
    assert result.model == MODEL_DISTILL


def test_embed_rejects_unlisted_model(fake_openai):
    q = LiveQwen(api_key="k")
    with pytest.raises(ValueError):
        q.embed("gpt-4o", ["hi"])


def test_embed_records_usage_and_returns_vectors(fake_openai):
    q = LiveQwen(api_key="k")
    vecs = q.embed(MODEL_EMBEDDING, ["door ajar", "power loss"])
    assert len(vecs) == 2 and vecs[0] == [0.1, 0.2]
    assert q.usage.totals()[MODEL_EMBEDDING]["calls"] == 1


def test_tts_rejects_unlisted_model(fake_openai):
    q = LiveQwen(api_key="k")
    with pytest.raises(ValueError):
        q.tts("gpt-4o", "hello", "calmly")


def test_tts_returns_audio_bytes(fake_openai):
    q = LiveQwen(api_key="k")
    audio = q.tts(MODEL_TTS, "fridge door open", "urgent but calm")
    assert audio == b"AUDIO-BYTES"


def test_usage_meter_totals_accumulate_across_calls():
    meter = UsageMeter()
    meter.record(MODEL_DIAGNOSIS, 10, 5)
    meter.record(MODEL_DIAGNOSIS, 20, 8)
    assert meter.totals()[MODEL_DIAGNOSIS] == {"calls": 2, "prompt_tokens": 30, "completion_tokens": 13}


# --------------------------------------------------------------------------- default_transport()

def test_default_transport_goes_live_only_when_flagged(monkeypatch):
    import permafrost.qwen as qwen_pkg

    sentinel = object()
    monkeypatch.setattr(qwen_pkg, "LiveQwen", lambda: sentinel)
    monkeypatch.setenv("PERMAFROST_LIVE", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-key-for-test")
    assert qwen_pkg.default_transport() is sentinel
