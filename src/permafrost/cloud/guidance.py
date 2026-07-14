"""Guidance store: CDC-style cold-chain snippets + embedding retrieval.

Every verdict must cite authority (invariant I4) — uncited advice reads as AI
guesswork to a compliance officer. Retrieval uses ``text-embedding-v4``
through the transport; offline, FakeQwen's hashing-trick vectors keep
retrieval deterministic *and* keyword-relevant.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from importlib import resources
from typing import Any

from ..qwen.transport import MODEL_EMBEDDING, QwenTransport

__all__ = ["Snippet", "GuidanceStore", "format_citation"]


@dataclass(frozen=True)
class Snippet:
    id: str
    source: str
    title: str
    text: str

    def embed_text(self) -> str:
        return f"{self.title}. {self.text}"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def format_citation(snippet: Snippet) -> str:
    body = snippet.text if len(snippet.text) <= 160 else snippet.text[:157] + "..."
    return f"{snippet.source} — {snippet.title}: {body}"


class GuidanceStore:
    """Embeds the committed corpus once, answers top-k queries deterministically."""

    def __init__(self, transport: QwenTransport):
        self._transport = transport
        raw = json.loads(
            resources.files("permafrost.cloud").joinpath("guidance_corpus.json").read_text("utf-8")
        )
        self.snippets: list[Snippet] = [Snippet(**s) for s in raw["snippets"]]
        self._vectors: list[list[float]] | None = None

    def _ensure_vectors(self) -> list[list[float]]:
        if self._vectors is None:
            self._vectors = self._transport.embed(
                MODEL_EMBEDDING, [s.embed_text() for s in self.snippets]
            )
        return self._vectors

    def retrieve(self, query: str, k: int = 2) -> list[tuple[Snippet, float]]:
        vectors = self._ensure_vectors()
        qv = self._transport.embed(MODEL_EMBEDDING, [query])[0]
        scored = [
            (snippet, _cosine(qv, vec)) for snippet, vec in zip(self.snippets, vectors)
        ]
        # deterministic tie-break on id
        scored.sort(key=lambda p: (-p[1], p[0].id))
        return scored[:k]

    def query_for(self, cause: str, features: dict[str, Any]) -> str:
        """Deterministic retrieval query per cause (keyword-bridged for hash embeddings)."""
        base = {
            "door_ajar": "door ajar left open gasket seal excursion alarm",
            "defrost_cycle": "automatic defrost cycle spike benign equipment periodic",
            "compressor_degradation": "compressor maintenance service drifting mean temperature aging cooling",
            "power_loss": "power failure outage protocol doors closed record temperature",
            "ambient_heat": "temperature excursion above 8 C storage unit assessment",
            "unknown": "temperature excursion documented assessed monitoring",
        }.get(cause, "temperature excursion vaccine storage")
        if features.get("peak_delta_c", 0.0) > 0:
            base += " vaccine storage 2-8 C excursion"
        return base
