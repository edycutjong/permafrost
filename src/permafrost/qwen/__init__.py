"""Qwen transport layer: LiveQwen (DashScope intl, OpenAI-compatible) + FakeQwen (offline)."""

from .fake import FakeQwen, classify_features, fixture_verdict, hash_embed
from .transport import (
    ALLOWED_MODELS,
    MODEL_DIAGNOSIS,
    MODEL_DISTILL,
    MODEL_EMBEDDING,
    MODEL_TTS,
    QWEN_BASE_URL,
    ChatResult,
    LiveQwen,
    QwenTransport,
    UsageMeter,
)

__all__ = [
    "ALLOWED_MODELS",
    "MODEL_DIAGNOSIS",
    "MODEL_DISTILL",
    "MODEL_EMBEDDING",
    "MODEL_TTS",
    "QWEN_BASE_URL",
    "ChatResult",
    "FakeQwen",
    "LiveQwen",
    "QwenTransport",
    "UsageMeter",
    "classify_features",
    "fixture_verdict",
    "hash_embed",
]


def default_transport() -> "QwenTransport":
    """LiveQwen when explicitly requested AND keyed; FakeQwen otherwise.

    Set ``PERMAFROST_LIVE=1`` and ``DASHSCOPE_API_KEY`` to go live. Tests and
    the judging path never set these.
    """
    import os

    if os.environ.get("PERMAFROST_LIVE") == "1" and os.environ.get("DASHSCOPE_API_KEY"):
        return LiveQwen()
    return FakeQwen()
