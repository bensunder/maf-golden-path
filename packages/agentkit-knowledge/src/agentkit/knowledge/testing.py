"""Offline knowledge tests: an in-memory stand-in for an Azure AI Search index.

    from agentkit.knowledge.testing import FakeSearchIndex, fake_embedder
    index = FakeSearchIndex([
        {"id": "policy-1", "doc_id": "refund-policy", "title": "Refund policy", "url": "https://intranet/refunds",
         "content": "Refunds over $50 need a lead's approval.", "groups": ["all-users"]},
    ])
    kb = KnowledgeBase(KnowledgeSettings(_env_file=None), search_client=index, embed=fake_embedder(),
                       groups=StaticGroups({"sam": ["support"]}))

The fake *evaluates* the security filter agentkit generates (``groups/any(g: search.in(...))``) and
``doc_id eq '...'`` filters, so tests prove trimming rather than assume it. Unknown filter syntax
raises, so a changed filter can't silently match everything. Ranking is simple word overlap.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

__all__ = ["FakeIndexClient", "FakeSearchIndex", "fake_embedder", "fake_knowledge", "index_folder"]

_ANY_IN = re.compile(r"^(\w+)/any\(g: search\.in\(g, '([^']*)', ','\)\)$")
_EQ = re.compile(r"^(\w+) eq '((?:[^']|'')*)'$")
_WORD = re.compile(r"[a-z0-9$]+")


def _matches(doc: dict[str, Any], expression: str | None) -> bool:
    if not expression:
        return True
    clauses = [c.strip() for c in re.split(r"\s+and\s+", expression)]
    for clause in clauses:
        if m := _ANY_IN.match(clause):
            field, values = m.group(1), set(m.group(2).split(","))
            if not set(doc.get(field) or []) & values:
                return False
        elif m := _EQ.match(clause):
            field, value = m.group(1), m.group(2).replace("''", "'")
            if str(doc.get(field)) != value:
                return False
        else:
            raise ValueError(f"FakeSearchIndex can't evaluate filter clause: {clause!r}")
    return True


class _Results:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = docs

    def __aiter__(self):
        async def gen():
            for doc in self._docs:
                yield doc

        return gen()


class FakeSearchIndex:
    def __init__(self, docs: Iterable[dict[str, Any]] = ()):
        self.docs: dict[str, dict[str, Any]] = {d["id"]: dict(d) for d in docs}
        self.searches: list[dict[str, Any]] = []
        self.fail: Exception | None = None  # set to simulate an outage

    # ------------------------------------------------------------ queries
    async def search(self, search_text: str | None = None, **params: Any) -> _Results:
        self.searches.append({"search_text": search_text, **params})
        if self.fail:
            raise self.fail
        candidates = [d for d in self.docs.values() if _matches(d, params.get("filter"))]
        if search_text and search_text != "*":
            words = set(_WORD.findall(search_text.lower()))
            scored = []
            for doc in candidates:
                text = f"{doc.get('title', '')} {doc.get('content', '')}".lower()
                score = len(words & set(_WORD.findall(text)))
                if score:
                    scored.append((score, doc))
            candidates = [d for _, d in sorted(scored, key=lambda sd: -sd[0])]
        skip, top = params.get("skip") or 0, params.get("top") or 50
        page = candidates[skip: skip + top]
        select = params.get("select")
        return _Results([{k: v for k, v in d.items() if not select or k in select or k == "id"} for d in page])

    async def get_document_count(self) -> int:
        return len(self.docs)

    # ------------------------------------------------------------ writes (ingestion)
    async def merge_or_upload_documents(self, documents: list[dict[str, Any]]) -> list[Any]:
        for doc in documents:
            self.docs[doc["id"]] = {**self.docs.get(doc["id"], {}), **doc}
        return [type("R", (), {"succeeded": True, "key": d["id"]})() for d in documents]

    upload_documents = merge_or_upload_documents

    async def delete_documents(self, documents: list[dict[str, Any]]) -> list[Any]:
        for doc in documents:
            self.docs.pop(doc["id"], None)
        return [type("R", (), {"succeeded": True, "key": d["id"]})() for d in documents]

    async def close(self) -> None:
        return None


class FakeIndexClient:
    """Stands in for ``SearchIndexClient``: records the index definitions it's given."""

    def __init__(self) -> None:
        self.indexes: dict[str, Any] = {}

    async def create_or_update_index(self, index: Any) -> Any:
        self.indexes[index.name] = index
        return index

    async def close(self) -> None:
        return None


def fake_embedder(dimensions: int = 8):
    """Deterministic embeddings (hash of words), so vector code paths run offline."""

    async def embed(text: str) -> list[float]:
        vector = [0.0] * dimensions
        for word in _WORD.findall(text.lower()):
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % dimensions] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    return embed


def index_folder(path: str | Path, *, knowledge: Any = None) -> FakeSearchIndex:
    """Run the real ingestion pipeline (acl.yaml, extraction, chunking) over a folder into a fake index.
    Use it in a fixture so offline tests and evals search your actual documents with their actual access."""
    from .ingest import ingest, local_documents
    from .search import KnowledgeSettings

    index = FakeSearchIndex()
    embed = fake_embedder()

    async def embed_many(texts: list[str]) -> list[list[float]]:
        return [await embed(t) for t in texts]

    async def run() -> None:
        report = await ingest(local_documents(path), search_client=index, embed_many=embed_many,
                              knowledge=knowledge or KnowledgeSettings(_env_file=None))
        if not report.ok:
            raise AssertionError(report.summary())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run())
    else:  # called from async code (an async test or fixture): run on a helper thread
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pool.submit(asyncio.run, run()).result()
    return index


@contextmanager
def fake_knowledge(kb: Any, index: FakeSearchIndex, *, groups: Any = None) -> Iterator[FakeSearchIndex]:
    """Point a ``KnowledgeBase`` at a fake index (and, optionally, a fake group directory) for the block."""
    saved = (kb._search_client, kb._embed, kb._groups)
    kb._search_client, kb._embed = index, fake_embedder()
    if groups is not None:
        kb._groups = groups
    try:
        yield index
    finally:
        kb._search_client, kb._embed, kb._groups = saved
