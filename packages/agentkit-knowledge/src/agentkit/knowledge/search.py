"""Permission-trimmed retrieval from Azure AI Search.

Every chunk in the index carries the Entra group ids allowed to read it (``groups``). A query runs
with a filter built from the *caller's* groups, so a user only ever retrieves what they may read.
Documents readable by everyone carry the ``public_group`` marker.

Fails closed: no known caller, a directory lookup error or a search error all mean *no documents*,
never *all documents*.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic_settings import BaseSettings, SettingsConfigDict

from agentkit.hosting import AgentKitSettings

__all__ = [
    "GraphGroups",
    "GroupResolver",
    "KnowledgeBase",
    "KnowledgeSettings",
    "KnowledgeUnavailable",
    "Passage",
    "StaticGroups",
    "gateway_embedder",
    "security_filter",
]

logger = logging.getLogger(__name__)

_SAFE_GROUP = re.compile(r"^[A-Za-z0-9._@:-]{1,128}$")
Embedder = Callable[[str], Awaitable[list[float]]]


class KnowledgeSettings(BaseSettings):
    """``AGENTKIT_KNOWLEDGE_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="AGENTKIT_KNOWLEDGE_", env_file=".env", extra="ignore")

    search_endpoint: str | None = None
    index: str = "knowledge"
    top_k: int = 5
    max_passage_chars: int = 1_200
    max_total_chars: int = 6_000
    #: Embedding deployment behind the AI gateway (used for queries and ingestion). Empty = keyword only.
    embedding_model: str | None = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    #: Semantic ranker configuration in the index. Empty = no semantic reranking.
    semantic_configuration: str | None = "default"
    #: ``groups``: trim by the caller's Entra groups (default). ``public``: everyone may read everything
    #: in this index (say so explicitly; there is no silent fallback to it).
    access: Literal["groups", "public"] = "groups"
    groups_field: str = "groups"
    public_group: str = "all-users"
    #: Azure AI Document Intelligence endpoint, for PDF and Office files during ingestion.
    docintel_endpoint: str | None = None


class KnowledgeUnavailable(Exception):
    """Retrieval can't run safely right now (unknown caller, directory or search failure)."""


@dataclass(frozen=True)
class Passage:
    doc_id: str
    chunk_id: str
    title: str
    url: str | None
    text: str
    score: float | None = None


# ---------------------------------------------------------------------------- groups
class GroupResolver(Protocol):
    async def groups(self, user_id: str) -> set[str]: ...


class StaticGroups:
    """A fixed user → groups map (tests, demos)."""

    def __init__(self, mapping: Mapping[str, Iterable[str]]):
        self._mapping = {k: set(v) for k, v in mapping.items()}

    async def groups(self, user_id: str) -> set[str]:
        return set(self._mapping.get(user_id, set()))


class GraphGroups:
    """The caller's Entra groups, including nested ones, from Microsoft Graph
    (``/users/{id}/transitiveMemberOf``) with the service's managed identity.

    Works for every channel: Teams turns carry an object id but no user token. Needs the
    ``GroupMember.Read.All`` application permission (the same one Teams approvers use). ``user_id`` may be
    an object id or a UPN. Cached for ``ttl`` seconds; errors raise (the caller fails closed)."""

    def __init__(self, *, client: Any = None, ttl: float = 300.0, max_pages: int = 20):
        from agentkit.tools import ApiClient, ManagedIdentityAuth

        self.client = client or ApiClient(
            "https://graph.microsoft.com/v1.0", auth=ManagedIdentityAuth("https://graph.microsoft.com/.default")
        )
        self._ttl = ttl
        self._max_pages = max_pages
        self._cache: dict[str, tuple[float, set[str]]] = {}

    async def groups(self, user_id: str) -> set[str]:
        hit = self._cache.get(user_id)
        if hit and hit[0] > time.monotonic():
            return set(hit[1])
        found: set[str] = set()
        path: str | None = f"/users/{user_id}/transitiveMemberOf/microsoft.graph.group"
        params: dict[str, Any] | None = {"$select": "id", "$top": 999}
        for _ in range(self._max_pages):
            page = await self.client.request("GET", path, params=params)
            found.update(str(g["id"]) for g in (page or {}).get("value", []) if g.get("id"))
            next_link = (page or {}).get("@odata.nextLink")
            if not next_link:
                break
            # ApiClient passes params explicitly, which would replace the link's query string (the
            # $skiptoken) and fetch page one forever; carry the link's query over as params instead.
            from urllib.parse import parse_qsl, urlsplit

            parts = urlsplit(next_link)
            path, params = parts.path.split("/v1.0", 1)[-1], dict(parse_qsl(parts.query))
        else:
            raise KnowledgeUnavailable(f"too many groups for {user_id}")  # incomplete list: don't guess
        self._cache[user_id] = (time.monotonic() + self._ttl, found)
        return set(found)


def security_filter(groups: Iterable[str], *, field: str = "groups", public_group: str = "all-users") -> str:
    """OData filter matching chunks readable by any of ``groups`` (or by everyone). Group ids that
    aren't plain identifiers are dropped, so nothing can be injected into the filter."""
    safe = sorted({g for g in groups if _SAFE_GROUP.match(g)} | {public_group})
    return f"{field}/any(g: search.in(g, '{','.join(safe)}', ','))"


# ---------------------------------------------------------------------------- retrieval
def gateway_embedder(settings: AgentKitSettings, model: str, *, dimensions: int | None = None) -> Embedder:
    """Embeddings through the AI gateway (same auth, headers and quotas as chat)."""
    from openai import AsyncAzureOpenAI, AsyncOpenAI

    from agentkit.hosting.clients import gateway_headers, token_provider

    if not settings.gateway_endpoint:
        raise ValueError("AGENTKIT_GATEWAY_ENDPOINT is not set")
    api_key = settings.api_key.get_secret_value() if settings.api_key else None
    provider = None if api_key else token_provider(settings)
    headers = gateway_headers(settings, "knowledge")
    if settings.gateway_style == "azure":
        client: Any = AsyncAzureOpenAI(azure_endpoint=settings.gateway_endpoint, api_version=settings.api_version,
                                       api_key=api_key, azure_ad_token_provider=provider, default_headers=headers)
    else:
        client = AsyncOpenAI(base_url=settings.gateway_endpoint.rstrip("/") + "/openai/v1",
                             api_key=api_key or provider, default_headers=headers)

    async def embed(text: str) -> list[float]:
        kwargs: dict[str, Any] = {"model": model, "input": [text]}
        if dimensions:
            kwargs["dimensions"] = dimensions
        response = await client.embeddings.create(**kwargs)
        return list(response.data[0].embedding)

    return embed


class KnowledgeBase:
    """One index to search, as a given user.

    ``search_client`` (an ``azure.search.documents.aio.SearchClient`` or a test fake), ``embed`` and
    ``groups`` are created from settings when not given: managed identity for search, the AI gateway for
    embeddings, Microsoft Graph for groups."""

    def __init__(
        self,
        knowledge: KnowledgeSettings | None = None,
        *,
        settings: AgentKitSettings | None = None,
        search_client: Any = None,
        embed: Embedder | None = None,
        groups: GroupResolver | None = None,
    ):
        self.knowledge = knowledge or KnowledgeSettings()
        self.settings = settings
        self._search_client = search_client
        self._embed = embed
        self._groups = groups

    # ------------------------------------------------------------ lazy dependencies
    def _settings(self) -> AgentKitSettings:
        if self.settings is None:
            self.settings = AgentKitSettings()
        return self.settings

    @property
    def search_client(self) -> Any:
        if self._search_client is None:
            from azure.search.documents.aio import SearchClient

            from agentkit.hosting import get_credential

            if not self.knowledge.search_endpoint:
                raise KnowledgeUnavailable("AGENTKIT_KNOWLEDGE_SEARCH_ENDPOINT is not set")
            credential = get_credential(self._settings())
            if credential is None:
                raise KnowledgeUnavailable("search needs an Entra credential (auth_mode api_key has none)")
            self._search_client = SearchClient(self.knowledge.search_endpoint, self.knowledge.index, credential)
        return self._search_client

    @property
    def embed(self) -> Embedder | None:
        if self._embed is None and self.knowledge.embedding_model:
            self._embed = gateway_embedder(self._settings(), self.knowledge.embedding_model,
                                           dimensions=self.knowledge.embedding_dimensions)
        return self._embed

    @property
    def group_resolver(self) -> GroupResolver:
        if self._groups is None:
            self._groups = GraphGroups()
        return self._groups

    # ------------------------------------------------------------ search
    async def filter_for(self, user_id: str | None) -> str | None:
        k = self.knowledge
        if k.access == "public":
            return None
        if not user_id:
            raise KnowledgeUnavailable("no signed-in user")
        try:
            groups = await self.group_resolver.groups(user_id)
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            logger.warning("group lookup for %s failed: %s", user_id, exc)
            raise KnowledgeUnavailable("couldn't verify document access") from exc
        return security_filter(groups, field=k.groups_field, public_group=k.public_group)

    async def search(self, query: str, *, user_id: str | None) -> list[Passage]:
        k = self.knowledge
        security = await self.filter_for(user_id)
        params: dict[str, Any] = {
            "search_text": query,
            "top": k.top_k,
            "select": ["id", "doc_id", "title", "url", "content"],
        }
        if security:
            params["filter"] = security
        try:
            if self.embed is not None:
                from azure.search.documents.models import VectorizedQuery

                vector = await self.embed(query)
                params["vector_queries"] = [VectorizedQuery(vector=vector, k_nearest_neighbors=max(50, k.top_k),
                                                            fields="content_vector")]
                params["vector_filter_mode"] = "preFilter"  # trim before ranking, not after
            if k.semantic_configuration:
                params["query_type"] = "semantic"  # plain strings: v12 enums serialise badly
                params["semantic_configuration_name"] = k.semantic_configuration
            results = await self.search_client.search(**params)
            passages = []
            async for doc in results:
                passages.append(Passage(
                    doc_id=str(doc.get("doc_id") or doc.get("id")), chunk_id=str(doc.get("id")),
                    title=str(doc.get("title") or doc.get("doc_id") or ""), url=doc.get("url") or None,
                    text=str(doc.get("content") or ""),
                    score=doc.get("@search.reranker_score") or doc.get("@search.score"),
                ))
            return passages
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            logger.warning("knowledge search failed: %s", exc)
            raise KnowledgeUnavailable("document search is unavailable") from exc
