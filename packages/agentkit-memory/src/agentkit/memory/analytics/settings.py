"""Analytics settings (``AGENTKIT_ANALYTICS_*``) and the one place the analytics store is built from them.

Off by default. ``AGENTKIT_ANALYTICS_ENABLED=true`` gives the agent an in-process DuckDB file with its audit
trail (``agent_episodes``) and a searchable copy of its memory (``semantic_memory``); see docs/analytics.md.
"""

from __future__ import annotations

import atexit
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .embeddings import embedder_from_settings
from .episodes import EpisodeMiddleware
from .indexer import SemanticIndexer, reindex
from .provider import AnalyticsProvider
from .sqltool import DatasetCatalog, parse_datasets
from .store import AnalyticsStore

__all__ = ["Analytics", "AnalyticsSettings", "analytics", "reset_analytics"]

logger = logging.getLogger(__name__)


class AnalyticsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTKIT_ANALYTICS_", env_file=".env", extra="ignore")

    enabled: bool = False
    #: Folder for the database file (``<agent>.duckdb``). One process writes it; give each replica its own disk.
    dir: str = "data/analytics"
    #: ``off``: episodes keep metadata only (timing, outcome, tokens, tool names). ``redacted``: also the input,
    #: prompt messages, output and tool arguments, after PII redaction. Raw text is never stored.
    capture: Literal["off", "redacted"] = "off"
    retention_days: int = Field(default=30, ge=1, le=3650)
    memory_limit: str = Field(default="256MB", pattern=r"^\d+(KB|MB|GB)$")
    threads: int = Field(default=2, ge=1, le=16)
    queue_size: int = Field(default=10_000, ge=100, le=1_000_000)

    #: Vectors for semantic search, computed on this host: ``none`` (word matching only), ``ollama`` or
    #: ``fastembed`` (in-process ONNX; install agentkit-memory[fastembed]).
    embeddings: Literal["none", "ollama", "fastembed"] = "none"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "nomic-embed-text"
    fastembed_model: str = "BAAI/bge-small-en-v1.5"
    fastembed_cache_dir: str | None = None

    #: Where Parquet exports go: an absolute folder or ``s3://bucket/prefix``. Empty: no exports.
    export_uri: str = ""
    export_interval_minutes: int = Field(default=60, ge=1, le=1440)
    #: S3-compatible endpoint (MinIO, R2, ...); empty for AWS. Credentials come from the usual AWS_* variables.
    s3_endpoint_url: str = ""
    s3_region: str = ""

    #: Parquet datasets the agent may query with ``query_analytics``: ``name=uri`` pairs, comma-separated.
    datasets: str = ""
    query_max_rows: int = Field(default=200, ge=1, le=10_000)
    query_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    query_memory_limit: str = Field(default="512MB", pattern=r"^\d+(KB|MB|GB)$")


@dataclass
class Analytics:
    store: AnalyticsStore
    provider: AnalyticsProvider
    middleware: EpisodeMiddleware
    catalog: DatasetCatalog | None
    settings: AnalyticsSettings

    def close(self) -> None:
        self.store.close()
        if self.catalog is not None:
            self.catalog.close()


_LOCK = threading.Lock()
_BUILT: dict[tuple[Any, ...], Analytics] = {}


def reset_analytics() -> None:
    """Close and forget built stores (tests)."""
    with _LOCK:
        for built in _BUILT.values():
            built.close()
        _BUILT.clear()


atexit.register(reset_analytics)


def _s3(cfg: AnalyticsSettings) -> Any:
    try:
        import s3fs
    except ImportError as exc:
        raise ValueError("s3:// exports or datasets need agentkit-memory[duckdb] (s3fs)") from exc
    client: dict[str, Any] = {}
    if cfg.s3_endpoint_url:
        client["endpoint_url"] = cfg.s3_endpoint_url
    if cfg.s3_region:
        client["region_name"] = cfg.s3_region
    return s3fs.S3FileSystem(client_kwargs=client, skip_instance_cache=True)


def analytics(settings: Any = None, cfg: AnalyticsSettings | None = None, *, memory: Any = None,
              agent_memory: Any = None) -> Analytics | None:
    """The process's analytics for this agent, or None when it is off. ``memory`` (a ``MemoryService``) gets the
    semantic indexer, so lessons, subject events and user notes become searchable; with ``agent_memory`` the
    lessons and subject events already stored are copied in too (per tenant, on its first run)."""
    if settings is None:
        from agentkit.hosting import AgentKitSettings

        settings = AgentKitSettings()
    cfg = cfg or AnalyticsSettings()
    if not cfg.enabled:
        return None
    agent = re.sub(r"[^A-Za-z0-9_.-]", "-", settings.service_name)[:64]
    path = str(Path(cfg.dir) / f"{agent}.duckdb")
    key = (path, id(memory))
    with _LOCK:
        built = _BUILT.get(key)
        if built is not None:
            return built
        try:
            import duckdb  # noqa: F401
        except ImportError as exc:
            raise ValueError("AGENTKIT_ANALYTICS_ENABLED needs agentkit-memory[duckdb]") from exc
        datasets = parse_datasets(cfg.datasets) if cfg.datasets else {}
        needs_s3 = cfg.export_uri.startswith("s3://") or any(u.startswith("s3://") for u in datasets.values())
        fs = _s3(cfg) if needs_s3 else None
        if cfg.export_uri and not (cfg.export_uri.startswith("s3://") or cfg.export_uri.startswith("/")):
            raise ValueError("AGENTKIT_ANALYTICS_EXPORT_URI must be an absolute folder or s3://bucket/prefix")
        embedder = embedder_from_settings(cfg.embeddings, ollama_url=cfg.ollama_url, ollama_model=cfg.ollama_model,
                                          fastembed_model=cfg.fastembed_model, cache_dir=cfg.fastembed_cache_dir)
        store = AnalyticsStore(path, agent=agent, embedder=embedder, retention_days=cfg.retention_days,
                               memory_limit=cfg.memory_limit, threads=cfg.threads, queue_size=cfg.queue_size,
                               export_uri=cfg.export_uri or None, export_interval_seconds=cfg.export_interval_minutes * 60,
                               filesystem=fs)
        try:
            store.start()
        except Exception as exc:  # e.g. another process has the file open: one writer per file
            raise ValueError(f"can't open {path}: {type(exc).__name__}: {str(exc).splitlines()[0][:200]}") from exc
        catalog = DatasetCatalog(datasets, filesystem=fs, max_rows=cfg.query_max_rows,
                                 timeout_seconds=cfg.query_timeout_seconds,
                                 memory_limit=cfg.query_memory_limit) if datasets else None
        if catalog is not None:  # read the datasets' schemas now, off the request path
            threading.Thread(target=catalog.describe, name="agentkit-analytics-datasets", daemon=True).start()
        pseudonym = getattr(memory, "pseudonym", None)
        user_key = None
        if memory is not None:
            from ..service import MemoryUser

            memory.indexer = SemanticIndexer(store)
            user_key = lambda user_id, tenant_id: memory.key_for(MemoryUser(user_id=user_id, tenant_id=tenant_id))
        provider = AnalyticsProvider(store, catalog=catalog, user_key=user_key)
        if agent_memory is not None and agent_memory.memory is memory:
            provider.backfill = lambda tenant: reindex(agent_memory, tenant)
        built = Analytics(store=store, catalog=catalog, settings=cfg, provider=provider,
                          middleware=EpisodeMiddleware(store, capture=cfg.capture, pseudonym=pseudonym))
        _BUILT[key] = built
        logger.info("analytics on: %s (capture=%s, embeddings=%s, datasets=%s)", path, cfg.capture,
                    getattr(embedder, "model", "none"), ",".join(datasets) or "none")
        return built
