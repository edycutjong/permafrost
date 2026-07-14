"""Qwen transport abstraction.

``LiveQwen`` talks to Qwen Cloud through the OpenAI-compatible endpoint::

    base_url = https://dashscope-intl.aliyuncs.com/compatible-mode/v1
    api key  = env DASHSCOPE_API_KEY

Models used by Permafrost (the only names this codebase will ever send):

- ``qwen3.7-plus``            curve diagnosis (with the thinking flag)
- ``qwen3.6-flash``           rule distillation
- ``text-embedding-v4``       guidance retrieval embeddings
- ``qwen3-tts-instruct-flash`` spoken alert (live call not exercised — README Status)

``FakeQwen`` (qwen/fake.py) implements the same protocol deterministically so
every test and the whole judging path run offline and keyless.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "QWEN_BASE_URL",
    "MODEL_DIAGNOSIS",
    "MODEL_DISTILL",
    "MODEL_EMBEDDING",
    "MODEL_TTS",
    "ALLOWED_MODELS",
    "ChatResult",
    "QwenTransport",
    "LiveQwen",
    "UsageMeter",
]

QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

MODEL_DIAGNOSIS = "qwen3.7-plus"
MODEL_DISTILL = "qwen3.6-flash"
MODEL_EMBEDDING = "text-embedding-v4"
MODEL_TTS = "qwen3-tts-instruct-flash"
ALLOWED_MODELS = frozenset({MODEL_DIAGNOSIS, MODEL_DISTILL, MODEL_EMBEDDING, MODEL_TTS})


@dataclass(frozen=True)
class ChatResult:
    content: str
    task_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    thinking: bool = False


@dataclass
class UsageMeter:
    """Token accounting per model — feeds the $/day economics in bench.py."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        self.calls.append(
            {"model": model, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
        )

    def totals(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for c in self.calls:
            slot = out.setdefault(c["model"], {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
            slot["calls"] += 1
            slot["prompt_tokens"] += c["prompt_tokens"]
            slot["completion_tokens"] += c["completion_tokens"]
        return out


class QwenTransport(Protocol):
    usage: UsageMeter

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        thinking: bool = False,
        json_response: bool = True,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult: ...

    def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...

    def tts(self, model: str, text: str, instructions: str) -> bytes: ...


def _check_model(model: str) -> None:
    if model not in ALLOWED_MODELS:
        raise ValueError(f"model {model!r} is not in Permafrost's allowed set {sorted(ALLOWED_MODELS)}")


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class LiveQwen:
    """Qwen Cloud via the OpenAI SDK against the DashScope-compatible endpoint.

    Constructed lazily and never during tests: every test path uses FakeQwen.
    """

    def __init__(self, api_key: str | None = None, base_url: str = QWEN_BASE_URL):
        key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        if not key:
            raise RuntimeError(
                "LiveQwen needs DASHSCOPE_API_KEY in the environment "
                "(offline judging path: FakeQwen, the default)"
            )
        from openai import OpenAI  # lazy: replay/test paths never import-time-need it

        self.base_url = base_url
        self._client = OpenAI(api_key=key, base_url=base_url)
        self.usage = UsageMeter()

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        thinking: bool = False,
        json_response: bool = True,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        _check_model(model)
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if json_response:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = tools
        # DashScope compatible-mode thinking switch for qwen3.x chat models
        kwargs["extra_body"] = {"enable_thinking": bool(thinking)}
        resp = self._client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        p_tok = getattr(usage, "prompt_tokens", None) or _estimate_tokens(str(messages))
        c_tok = getattr(usage, "completion_tokens", None) or _estimate_tokens(content)
        self.usage.record(model, p_tok, c_tok)
        return ChatResult(
            content=content,
            task_id=getattr(resp, "id", "") or "",
            model=model,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            thinking=thinking,
        )

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        _check_model(model)
        resp = self._client.embeddings.create(model=model, input=texts)
        self.usage.record(model, sum(_estimate_tokens(t) for t in texts), 0)
        return [d.embedding for d in resp.data]

    def tts(self, model: str, text: str, instructions: str) -> bytes:
        """Spoken alert via qwen3-tts-instruct-flash.

        STATUS: implemented against the OpenAI-compatible audio surface but a
        live call has NOT been exercised in this build (see README Status).
        """
        _check_model(model)
        resp = self._client.audio.speech.create(  # pragma: no cover - live only
            model=model, voice="alloy", input=text, instructions=instructions
        )
        self.usage.record(model, _estimate_tokens(text + instructions), 0)  # pragma: no cover
        return resp.read()  # pragma: no cover
