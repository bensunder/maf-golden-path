"""DuckDB analytics for agents: an audit trail of every run (``agent_episodes``), a searchable copy of what the
agent remembers (``semantic_memory``, hybrid SQL + vector search), Parquet exports to a folder or S3, and a
sandboxed read-only SQL tool over Parquet datasets. Off by default; see docs/analytics.md.

Needs ``agentkit-memory[duckdb]`` (and ``[fastembed]`` for in-process embeddings).
"""

from .embeddings import (
    Embedder,
    EmbedError,
    FastEmbedEmbedder,
    OllamaEmbedder,
    embedder_from_settings,
)
from .episodes import EpisodeMiddleware, ToolTimingMiddleware, redact
from .indexer import SemanticIndexer
from .provider import QUERY_TOOL, SEARCH_TOOL, AnalyticsProvider
from .settings import Analytics, AnalyticsSettings, analytics, reset_analytics
from .sqlguard import SqlRejected, check_select
from .sqltool import DatasetCatalog, QueryFailed, parse_datasets
from .store import AnalyticsStore, Episode, MemoryRecord, SearchHit, ToolCallRecord

__all__ = [
    "QUERY_TOOL",
    "SEARCH_TOOL",
    "Analytics",
    "AnalyticsProvider",
    "AnalyticsSettings",
    "AnalyticsStore",
    "DatasetCatalog",
    "EmbedError",
    "Embedder",
    "Episode",
    "EpisodeMiddleware",
    "FastEmbedEmbedder",
    "MemoryRecord",
    "OllamaEmbedder",
    "QueryFailed",
    "SearchHit",
    "SemanticIndexer",
    "SqlRejected",
    "ToolCallRecord",
    "ToolTimingMiddleware",
    "analytics",
    "check_select",
    "embedder_from_settings",
    "parse_datasets",
    "redact",
    "reset_analytics",
]
