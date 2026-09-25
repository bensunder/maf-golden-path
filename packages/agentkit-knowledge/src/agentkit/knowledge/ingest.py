"""Load documents into the knowledge index: read, set access, extract, chunk, embed, sync.

    agentkit-ingest --source ./knowledge                       # a folder (local runs, tests)
    agentkit-ingest --source https://<acct>.blob.core.windows.net/knowledge   # a Blob container
    agentkit-ingest --source ./knowledge --dry-run             # show what would change

Who may read each document comes from an ``acl.yaml`` at the root of the source (first matching rule
wins) or, for blobs, the ``agentkit_groups`` metadata (comma-separated group ids). **A document with no
matching rule is skipped, not published to everyone.**

    # acl.yaml
    rules:
      - match: "leads/**"
        groups: ["<refund-leads group object id>"]
      - match: "**"
        groups: ["all-users"]          # the index's public marker (AGENTKIT_KNOWLEDGE_PUBLIC_GROUP)

Markdown, text and HTML are read directly; PDF and Office files go through Azure AI Document
Intelligence (layout model, Markdown output) when ``AGENTKIT_KNOWLEDGE_DOCINTEL_ENDPOINT`` is set.
Sync is incremental: unchanged documents (same content hash and access) are skipped, changed ones are
re-chunked, and documents that disappeared from the source are removed from the index.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html.parser
import json
import logging
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .search import _SAFE_GROUP, KnowledgeSettings

__all__ = [
    "AclRules",
    "Chunk",
    "IngestReport",
    "SourceDocument",
    "chunk_markdown",
    "index_definition",
    "ingest",
    "local_documents",
]

logger = logging.getLogger(__name__)

TEXT_TYPES = {".md", ".markdown", ".txt", ".html", ".htm"}
DOCINTEL_TYPES = {".pdf", ".docx", ".pptx", ".xlsx"}
EmbedMany = Callable[[list[str]], Awaitable[list[list[float]]]]


# ---------------------------------------------------------------------------- documents and access
@dataclass
class SourceDocument:
    path: str  # relative, forward slashes
    data: bytes
    groups: list[str]
    title: str | None = None
    url: str | None = None

    @property
    def doc_id(self) -> str:
        stem = str(PurePosixPath(self.path).with_suffix(""))
        return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "doc"

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.path).suffix.lower()

    @property
    def fingerprint(self) -> str:
        """Content + access: a changed ACL re-indexes the document even if its text didn't change."""
        digest = hashlib.sha256(self.data)
        digest.update(json.dumps(sorted(self.groups)).encode())
        digest.update((self.url or "").encode())
        return digest.hexdigest()[:32]


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Path-aware glob, like .gitignore: ``*`` and ``?`` stay inside one folder, ``**`` crosses folders.
    (``fnmatch`` lets ``*`` match ``/``, so ``*.md`` would also grant access to ``leads/secret.md``.)"""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


class AclRules:
    def __init__(self, rules: Iterable[dict[str, Any]] = ()):
        self.rules = [(_glob_regex(str(r["match"])), [str(g) for g in r.get("groups") or []]) for r in rules]

    @classmethod
    def from_yaml(cls, text: str | None) -> AclRules:
        data = yaml.safe_load(text or "") or {}
        return cls(data.get("rules") or [])

    def groups_for(self, path: str) -> list[str] | None:
        for pattern, groups in self.rules:
            if pattern.match(path):
                return groups
        return None


def _front_matter(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            try:
                meta = yaml.safe_load(text[4:end]) or {}
                return (meta if isinstance(meta, dict) else {}), text[end + 4:].lstrip("\n")
            except yaml.YAMLError:
                pass
    return {}, text


def local_documents(root: str | Path, *, url_base: str | None = None) -> list[SourceDocument | tuple[str, str]]:
    """Documents under ``root`` with their access groups; ``(path, reason)`` for skipped files."""
    root = Path(root)
    acl_file = root / "acl.yaml"
    rules = AclRules.from_yaml(acl_file.read_text(encoding="utf-8") if acl_file.exists() else None)
    out: list[SourceDocument | tuple[str, str]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel == "acl.yaml" or any(part.startswith(".") for part in PurePosixPath(rel).parts) or rel.endswith("README.md"):
            continue
        groups = rules.groups_for(rel)
        if groups is None:
            out.append((rel, "no acl.yaml rule matches: not indexed"))
            continue
        url = f"{url_base.rstrip('/')}/{rel}" if url_base else None
        out.append(SourceDocument(rel, path.read_bytes(), groups, url=url))
    return out


async def blob_documents(container_url: str, credential: Any, *, url_base: str | None = None
                         ) -> list[SourceDocument | tuple[str, str]]:
    """Documents in a Blob container (managed identity: Storage Blob Data Reader)."""
    from azure.storage.blob.aio import ContainerClient

    out: list[SourceDocument | tuple[str, str]] = []
    async with ContainerClient.from_container_url(container_url, credential=credential) as container:
        rules_text = None
        try:
            rules_text = (await (await container.download_blob("acl.yaml")).readall()).decode("utf-8")
        except Exception:
            pass
        rules = AclRules.from_yaml(rules_text)
        async for blob in container.list_blobs(include=["metadata"]):
            name = blob.name
            if name == "acl.yaml" or name.endswith("/") or name.endswith("README.md"):
                continue
            meta = {k.lower(): v for k, v in (blob.metadata or {}).items()}
            if meta.get("agentkit_groups"):
                groups = [g.strip() for g in meta["agentkit_groups"].split(",") if g.strip()]
            else:
                groups = rules.groups_for(name)
            if groups is None:
                out.append((name, "no acl.yaml rule or agentkit_groups metadata: not indexed"))
                continue
            data = await (await container.download_blob(name)).readall()
            url = f"{url_base.rstrip('/')}/{name}" if url_base else meta.get("agentkit_url")
            out.append(SourceDocument(name, data, groups, title=meta.get("agentkit_title"), url=url))
    return out


# ---------------------------------------------------------------------------- extraction
class _HtmlText(html.parser.HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title: str | None = None
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCK:
            self.parts.append("\n\n")
        if re.fullmatch(r"h[1-6]", tag):
            self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title = (self.title or "") + data.strip()
        elif not self._skip:
            self.parts.append(data)


class DocumentIntelligence:
    """Azure AI Document Intelligence layout model → Markdown (managed identity: Cognitive Services User)."""

    def __init__(self, endpoint: str, *, auth: Any = None, http: Any = None, poll_seconds: float = 1.0,
                 timeout_seconds: float = 300.0):
        import httpx

        from agentkit.tools import ManagedIdentityAuth

        self.endpoint = endpoint.rstrip("/")
        self.auth = auth or ManagedIdentityAuth("https://cognitiveservices.azure.com/.default")
        self.http = http or httpx.AsyncClient(timeout=60)
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    async def markdown(self, data: bytes) -> str:
        url = (f"{self.endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze"
               "?api-version=2024-11-30&outputContentFormat=markdown")
        headers = {**(await self.auth.headers()), "Content-Type": "application/octet-stream"}
        response = await self.http.post(url, content=data, headers=headers)
        if response.status_code != 202:
            raise RuntimeError(f"Document Intelligence returned {response.status_code}: {response.text[:200]}")
        operation = response.headers["operation-location"]
        waited = 0.0
        while waited < self.timeout_seconds:
            await asyncio.sleep(self.poll_seconds)
            waited += self.poll_seconds
            poll = await self.http.get(operation, headers=await self.auth.headers())
            body = poll.json()
            if body.get("status") == "succeeded":
                return body["analyzeResult"]["content"]
            if body.get("status") == "failed":
                raise RuntimeError(f"Document Intelligence failed: {body.get('error')}")
        raise TimeoutError("Document Intelligence did not finish in time")


async def extract(doc: SourceDocument, docintel: DocumentIntelligence | None) -> tuple[str, str]:
    """(title, markdown text) for a document."""
    fallback_title = PurePosixPath(doc.path).stem.replace("-", " ").replace("_", " ").strip().capitalize()
    if doc.suffix in (".md", ".markdown", ".txt"):
        meta, text = _front_matter(doc.data.decode("utf-8", errors="replace"))
        doc.url = doc.url or meta.get("url")
        heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        return str(meta.get("title") or doc.title or (heading.group(1).strip() if heading else fallback_title)), text
    if doc.suffix in (".html", ".htm"):
        parser = _HtmlText()
        parser.feed(doc.data.decode("utf-8", errors="replace"))
        text = re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()
        return doc.title or parser.title or fallback_title, text
    if doc.suffix in DOCINTEL_TYPES:
        if docintel is None:
            raise LookupError(f"{doc.suffix} needs Document Intelligence (set AGENTKIT_KNOWLEDGE_DOCINTEL_ENDPOINT)")
        text = await docintel.markdown(doc.data)
        heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        return doc.title or (heading.group(1).strip() if heading else fallback_title), text
    raise LookupError(f"unsupported file type {doc.suffix or '(none)'}")


# ---------------------------------------------------------------------------- chunking
def chunk_markdown(text: str, *, max_chars: int = 1_500, overlap: int = 200) -> list[tuple[str, str]]:
    """Split Markdown into ``(heading path, text)`` chunks of at most ``max_chars``: by heading first,
    then by paragraph, then by sentence; consecutive chunks of one section share ``overlap`` chars."""
    sections: list[tuple[list[str], list[str]]] = [([], [])]
    path: list[str] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        block = block.strip()
        if not block:
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", block.splitlines()[0])
        if heading:
            level = len(heading.group(1))
            path = path[: level - 1] + [heading.group(2).strip()]
            sections.append((list(path), []))
            rest = "\n".join(block.splitlines()[1:]).strip()
            if rest:
                sections[-1][1].append(rest)
        else:
            sections[-1][1].append(block)

    limit = max(100, max_chars - overlap)  # leave room for the overlap carried into the next chunk

    def pieces(paragraph: str) -> list[str]:
        if len(paragraph) <= limit:
            return [paragraph]
        out, current = [], ""
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            while len(sentence) > limit:  # one enormous "sentence": hard cut
                out.append(sentence[:limit])
                sentence = sentence[limit:]
            if current and len(current) + 1 + len(sentence) > limit:
                out.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            out.append(current)
        return out

    chunks: list[tuple[str, str]] = []
    for heading_path, paragraphs in sections:
        label = " › ".join(heading_path)
        current = ""
        for paragraph in paragraphs:
            for piece in pieces(paragraph):
                if current and len(current) + 2 + len(piece) > max_chars:
                    chunks.append((label, current))
                    tail = current[-overlap:] if overlap else ""
                    tail = tail[tail.find(" ") + 1:] if " " in tail else tail
                    current = (tail + "\n\n" + piece).strip() if len(tail) + 2 + len(piece) <= max_chars else piece
                else:
                    current = f"{current}\n\n{piece}".strip()
        if current:
            chunks.append((label, current))
    return chunks


# ---------------------------------------------------------------------------- index
def index_definition(name: str, *, dimensions: int = 1536, semantic_configuration: str | None = "default",
                     groups_field: str = "groups"):
    """The index agentkit's knowledge tool expects (vector field ``content_vector``, HNSW, semantic config)."""
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration,
        SearchableField,
        SearchField,
        SearchFieldDataType,
        SearchIndex,
        SemanticConfiguration,
        SemanticField,
        SemanticPrioritizedFields,
        SemanticSearch,
        SimpleField,
        VectorSearch,
        VectorSearchProfile,
    )

    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SimpleField(name="doc_id", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="title", type=SearchFieldDataType.String),
        SimpleField(name="url", type=SearchFieldDataType.String),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SimpleField(name=groups_field, type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                    filterable=True),
        SimpleField(name="doc_hash", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="chunk", type=SearchFieldDataType.Int32),
        SearchField(name="content_vector", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                    searchable=True, vector_search_dimensions=dimensions, vector_search_profile_name="default"),
    ]
    vector = VectorSearch(algorithms=[HnswAlgorithmConfiguration(name="hnsw")],
                          profiles=[VectorSearchProfile(name="default", algorithm_configuration_name="hnsw")])
    semantic = None
    if semantic_configuration:
        semantic = SemanticSearch(configurations=[SemanticConfiguration(
            name=semantic_configuration,
            prioritized_fields=SemanticPrioritizedFields(title_field=SemanticField(field_name="title"),
                                                         content_fields=[SemanticField(field_name="content")]),
        )])
    return SearchIndex(name=name, fields=fields, vector_search=vector, semantic_search=semantic)


# ---------------------------------------------------------------------------- sync
@dataclass
class IngestReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    chunks: int = 0

    @property
    def ok(self) -> bool:
        return not any("failed" in reason for _, reason in self.skipped)

    def summary(self) -> str:
        lines = [f"added {len(self.added)}, updated {len(self.updated)}, unchanged {len(self.unchanged)}, "
                 f"removed {len(self.removed)}, skipped {len(self.skipped)}; {self.chunks} chunks written"]
        lines += [f"  skipped {path}: {reason}" for path, reason in self.skipped]
        return "\n".join(lines)


async def _existing(search_client: Any) -> dict[str, str]:
    """doc_id -> doc_hash currently in the index."""
    found: dict[str, str] = {}
    skip = 0
    while True:
        results = await search_client.search(search_text="*", select=["id", "doc_id", "doc_hash"], top=1000, skip=skip)
        page = [doc async for doc in results]
        for doc in page:
            found.setdefault(str(doc["doc_id"]), str(doc.get("doc_hash") or ""))
        if len(page) < 1000:
            return found
        skip += 1000


async def _chunk_ids(search_client: Any, doc_id: str) -> list[str]:
    escaped = doc_id.replace("'", "''")
    results = await search_client.search(search_text="*", filter=f"doc_id eq '{escaped}'", select=["id"], top=1000)
    return [str(doc["id"]) async for doc in results]


async def ingest(
    documents: Iterable[SourceDocument | tuple[str, str]],
    *,
    search_client: Any,
    embed_many: EmbedMany | None,
    index_client: Any = None,
    knowledge: KnowledgeSettings | None = None,
    docintel: DocumentIntelligence | None = None,
    prune: bool = True,
    dry_run: bool = False,
    max_chars: int = 1_500,
) -> IngestReport:
    knowledge = knowledge or KnowledgeSettings()
    report = IngestReport()
    if index_client is not None and not dry_run:
        await index_client.create_or_update_index(index_definition(
            knowledge.index, dimensions=knowledge.embedding_dimensions,
            semantic_configuration=knowledge.semantic_configuration or None, groups_field=knowledge.groups_field))
    existing = await _existing(search_client)
    seen: set[str] = set()  # ids met in the source (duplicate detection)
    kept: set[str] = set()  # ids indexed or unchanged; anything else in the index is removed (fail closed)
    for item in documents:
        if isinstance(item, tuple):
            report.skipped.append(item)
            continue
        doc = item
        bad = [g for g in doc.groups if not _SAFE_GROUP.match(g)]
        if not doc.groups or bad:
            report.skipped.append((doc.path, f"invalid or empty access groups {bad or doc.groups}: not indexed"))
            continue
        doc_id = doc.doc_id
        if doc_id in seen:
            report.skipped.append((doc.path, f"duplicate document id {doc_id!r} (rename the file)"))
            continue
        seen.add(doc_id)
        fingerprint = doc.fingerprint
        if existing.get(doc_id) == fingerprint:
            report.unchanged.append(doc_id)
            kept.add(doc_id)
            continue
        try:
            title, text = await extract(doc, docintel)
        except LookupError as exc:
            report.skipped.append((doc.path, str(exc)))
            continue
        except Exception as exc:
            report.skipped.append((doc.path, f"extraction failed: {exc}"))
            continue
        pieces = chunk_markdown(text, max_chars=max_chars)
        if not pieces:
            report.skipped.append((doc.path, "no text"))
            continue
        chunks = []
        for i, (section, body) in enumerate(pieces):
            parts = [p for p in section.split(" › ") if p] if section else []
            if parts and parts[0].strip().lower() == title.strip().lower():
                parts = parts[1:]  # the H1 usually repeats the document title
            chunk_title = " › ".join([title, *parts])
            chunks.append({"id": f"{doc_id}--{i:04d}", "doc_id": doc_id, "title": chunk_title, "url": doc.url,
                           "content": body, knowledge.groups_field: list(doc.groups), "doc_hash": fingerprint,
                           "chunk": i})
        (report.updated if doc_id in existing else report.added).append(doc_id)
        kept.add(doc_id)
        report.chunks += len(chunks)
        if dry_run:
            continue
        if embed_many is not None:
            for start in range(0, len(chunks), 16):
                batch = chunks[start:start + 16]
                vectors = await embed_many([f"{c['title']}\n\n{c['content']}" for c in batch])
                for chunk, vector in zip(batch, vectors):
                    chunk["content_vector"] = vector
        if doc_id in existing:
            stale = [i for i in await _chunk_ids(search_client, doc_id) if i not in {c["id"] for c in chunks}]
            if stale:
                await search_client.delete_documents([{"id": i} for i in stale])
        for start in range(0, len(chunks), 500):
            await search_client.merge_or_upload_documents(chunks[start:start + 500])
    # Documents gone from the source, or whose access rule was removed, or that failed to extract, leave the
    # index: stale chunks would keep their old access groups. The next successful run adds them back.
    if prune:
        for doc_id in sorted(set(existing) - kept):
            report.removed.append(doc_id)
            if not dry_run:
                ids = await _chunk_ids(search_client, doc_id)
                if ids:
                    await search_client.delete_documents([{"id": i} for i in ids])
    return report


def gateway_embed_many(settings: Any, model: str, *, dimensions: int | None = None) -> EmbedMany:
    """Batch embeddings through the AI gateway."""
    from .search import gateway_embedder

    single = gateway_embedder(settings, model, dimensions=dimensions)

    async def embed_many(texts: list[str]) -> list[list[float]]:
        return list(await asyncio.gather(*(single(t) for t in texts)))

    return embed_many


# ---------------------------------------------------------------------------- CLI
async def _main(args: argparse.Namespace) -> int:
    from agentkit.hosting import AgentKitSettings, get_credential

    knowledge = KnowledgeSettings()
    if args.index:
        knowledge.index = args.index
    endpoint = args.search_endpoint or knowledge.search_endpoint
    if not endpoint:
        print("set AGENTKIT_KNOWLEDGE_SEARCH_ENDPOINT or pass --search-endpoint", file=sys.stderr)
        return 2
    settings = AgentKitSettings()
    credential = get_credential(settings)
    if credential is None:
        print("ingestion needs an Entra credential (AGENTKIT_AUTH_MODE=managed_identity/azure_cli/default)",
              file=sys.stderr)
        return 2
    from azure.search.documents.aio import SearchClient
    from azure.search.documents.indexes.aio import SearchIndexClient

    if args.source.startswith("https://"):
        documents = await blob_documents(args.source, credential, url_base=args.url_base)
    else:
        documents = local_documents(args.source, url_base=args.url_base)
    embed_many = (gateway_embed_many(settings, knowledge.embedding_model, dimensions=knowledge.embedding_dimensions)
                  if knowledge.embedding_model else None)
    docintel = DocumentIntelligence(knowledge.docintel_endpoint) if knowledge.docintel_endpoint else None
    async with SearchClient(endpoint, knowledge.index, credential) as search_client, \
            SearchIndexClient(endpoint, credential) as index_client:
        report = await ingest(documents, search_client=search_client, index_client=index_client, embed_many=embed_many,
                              knowledge=knowledge, docintel=docintel, prune=not args.no_prune, dry_run=args.dry_run)
    print(("DRY RUN: " if args.dry_run else "") + report.summary())
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentkit-ingest", description="Sync documents into the knowledge index.")
    parser.add_argument("--source", required=True, help="folder, or https://<account>.blob.core.windows.net/<container>")
    parser.add_argument("--index", help="index name (default AGENTKIT_KNOWLEDGE_INDEX)")
    parser.add_argument("--search-endpoint", help="default AGENTKIT_KNOWLEDGE_SEARCH_ENDPOINT")
    parser.add_argument("--url-base", help="link citations to this base URL + the file's path (e.g. your intranet)")
    parser.add_argument("--no-prune", action="store_true", help="keep index documents missing from the source")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    logging.basicConfig(level=logging.WARNING)
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
