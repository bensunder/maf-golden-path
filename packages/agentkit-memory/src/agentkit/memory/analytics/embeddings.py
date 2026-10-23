"""Embeddings for semantic memory search, computed on this host (no per-call cost).

* ``OllamaEmbedder``: a local Ollama server (``POST /api/embed``), e.g. ``nomic-embed-text`` (768 dimensions).
* ``FastEmbedEmbedder``: in-process ONNX models through the ``fastembed`` package, e.g.
  ``BAAI/bge-small-en-v1.5`` (384 dimensions, ~130 MB, downloaded once on first use).
* No embedder: search is lexical only.

Embedding never sits on a write path: rows are stored first and embedded by a background worker. A search embeds
its query with a short timeout and falls back to lexical scoring when the embedder is slow or down. A circuit
breaker stops calling a failing embedder for a while instead of slowing every search.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from typing import Protocol

import httpx

__all__ = ["CircuitOpen", "EmbedError", "Embedder", "FastEmbedEmbedder", "OllamaEmbedder", "embedder_from_settings"]

logger = logging.getLogger(__name__)


class EmbedError(RuntimeError):
    pass


class CircuitOpen(EmbedError):
    pass


class Embedder(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, all of ``dimensions`` length. Raises ``EmbedError``."""
        ...


class _Breaker:
    def __init__(self, threshold: int = 3, cooldown: float = 60.0, clock=time.monotonic) -> None:
        self.threshold, self.cooldown, self.clock = threshold, cooldown, clock
        self.failures, self.open_until = 0, 0.0
        self.lock = threading.Lock()

    def check(self) -> None:
        with self.lock:
            if self.failures >= self.threshold and self.clock() < self.open_until:
                raise CircuitOpen("the embedder is failing; paused")

    def ok(self) -> None:
        with self.lock:
            self.failures = 0

    def fail(self) -> None:
        with self.lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.open_until = self.clock() + self.cooldown


class OllamaEmbedder:
    def __init__(self, url: str = "http://localhost:11434", model: str = "nomic-embed-text", *,
                 dimensions: int | None = None, timeout: float = 10.0, transport: httpx.BaseTransport | None = None) -> None:
        self.model = model
        self._client = httpx.Client(base_url=url.rstrip("/"), timeout=timeout, transport=transport)
        self._breaker = _Breaker()
        self.dimensions = dimensions or len(self.embed(["dimension probe"])[0])

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        self._breaker.check()
        try:
            response = self._client.post("/api/embed", json={"model": self.model, "input": list(texts)})
            response.raise_for_status()
            vectors = response.json()["embeddings"]
            if len(vectors) != len(texts):
                raise EmbedError("Ollama returned a different number of embeddings")
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            self._breaker.fail()
            raise EmbedError(f"Ollama embedding failed: {type(exc).__name__}") from exc
        self._breaker.ok()
        return [[float(x) for x in v] for v in vectors]


class FastEmbedEmbedder:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5", *, cache_dir: str | None = None) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise EmbedError("install agentkit-memory[fastembed] for local embeddings") from exc
        self.model = model
        self._lock = threading.Lock()  # ONNX sessions aren't safe to share across threads
        self._impl = TextEmbedding(model_name=model, cache_dir=cache_dir)
        self.dimensions = len(self.embed(["dimension probe"])[0])

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            with self._lock:
                return [[float(x) for x in v] for v in self._impl.embed(list(texts))]
        except Exception as exc:  # pragma: no cover - model runtime errors
            raise EmbedError(f"local embedding failed: {type(exc).__name__}") from exc


def embedder_from_settings(kind: str, *, ollama_url: str, ollama_model: str, fastembed_model: str,
                           cache_dir: str | None = None) -> Embedder | None:
    """The configured embedder, or None (lexical search only) when it is off or can't start."""
    if kind == "none":
        return None
    try:
        if kind == "ollama":
            return OllamaEmbedder(ollama_url, ollama_model)
        if kind == "fastembed":
            return FastEmbedEmbedder(fastembed_model, cache_dir=cache_dir)
    except EmbedError as exc:
        logger.error("embeddings are off: %s", exc)
        return None
    raise ValueError(f"unknown embeddings kind {kind!r}")
