"""Memory settings (``AGENTKIT_MEMORY_*``) and the one place a ``MemoryService`` is built from them.

The service's agent (``UserMemoryProvider``) and its HTTP API (``MemoryChannel``) must share one
``MemoryService`` so a write through either invalidates the other's cache. ``memory_service()``
returns the same instance for the same configuration in a process.
"""

from __future__ import annotations

import threading
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .policy import MemoryPolicy
from .service import MemoryService
from .stores import CosmosMemoryStore, InMemoryMemoryStore, MemoryStore, RedisMemoryStore

__all__ = ["MemorySettings", "memory_service", "reset_memory_services"]


class MemorySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTKIT_MEMORY_", env_file=".env", extra="ignore")

    #: ``inherit`` uses the session store's backend (and its Redis URL / Cosmos account).
    store: Literal["inherit", "memory", "redis", "cosmos"] = "inherit"
    #: Secret mixed into every memory key (HMAC), so stored keys can't be traced back to a user id.
    #: Required in prod. 16+ characters; never change it on a running service (memory would be orphaned).
    salt: SecretStr | None = None
    retention_days: int = Field(default=180, ge=1, le=3650)
    max_items: int = Field(default=50, ge=1, le=500)
    max_chars: int = Field(default=280, ge=20, le=1000)
    timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    cache_seconds: float = Field(default=15.0, ge=0, le=300)
    writes_per_minute: int = Field(default=30, ge=0, le=1000)
    #: Cosmos container for memory; defaults to the session container (ids never collide).
    cosmos_container: str | None = None
    #: Local and test only: the user to remember for when a request carries no identity (evals, a laptop).
    dev_user: str | None = None


_LOCK = threading.Lock()
_SERVICES: dict[tuple[Any, ...], MemoryService] = {}


def reset_memory_services() -> None:
    """Forget built services (tests)."""
    with _LOCK:
        _SERVICES.clear()


def _resolve(settings: Any, mem: MemorySettings) -> tuple[str, str, bool]:
    """(store kind, salt, salt_configured), enforcing the prod policy."""
    kind = settings.session_store if mem.store == "inherit" else mem.store
    problems: list[str] = []
    salt = mem.salt.get_secret_value() if mem.salt else ""
    if salt and len(salt) < 16:
        problems.append("AGENTKIT_MEMORY_SALT must be at least 16 characters")
    if settings.environment == "prod":
        if not salt:
            problems.append("AGENTKIT_MEMORY_SALT is required in prod")
        if kind == "memory":
            problems.append("the in-process memory store loses every memory on restart; use redis or cosmos in prod")
        if mem.dev_user:
            problems.append("AGENTKIT_MEMORY_DEV_USER is for local and test only")
    if kind == "redis" and not settings.redis_url:
        problems.append("the redis memory store needs AGENTKIT_REDIS_URL")
    if kind == "cosmos" and not (settings.cosmos_endpoint and (mem.cosmos_container or settings.cosmos_container)):
        problems.append("the cosmos memory store needs AGENTKIT_COSMOS_ENDPOINT and a container")
    if problems:
        raise ValueError("; ".join(problems))
    configured = bool(salt)
    if not salt:  # local/dev/test only (prod refused above): stable per service, but not secret
        salt = f"agentkit-memory-unsalted:{settings.service_name}".ljust(16, ".")
    return kind, salt, configured


def _build_store(kind: str, settings: Any, mem: MemorySettings, credential: Any) -> MemoryStore:
    if kind == "memory":
        return InMemoryMemoryStore()
    if kind == "redis":
        return RedisMemoryStore.from_url(settings.redis_url.get_secret_value(),
                                         prefix=getattr(settings, "session_key_prefix", "agentkit:"))
    from azure.cosmos.aio import CosmosClient

    if credential is None:
        from agentkit.hosting.clients import get_credential

        credential = get_credential(settings)
    client = CosmosClient(settings.cosmos_endpoint, credential=credential)
    container = client.get_database_client(settings.cosmos_database).get_container_client(
        mem.cosmos_container or settings.cosmos_container)
    return CosmosMemoryStore(container)


def memory_service(settings: Any = None, memory_settings: MemorySettings | None = None, *,
                   credential: Any = None) -> MemoryService:
    """The process's ``MemoryService`` for this configuration (built once, then shared)."""
    if settings is None:
        from agentkit.hosting import AgentKitSettings

        settings = AgentKitSettings()
    mem = memory_settings or MemorySettings()
    kind, salt, configured = _resolve(settings, mem)
    redis_url = settings.redis_url.get_secret_value() if getattr(settings, "redis_url", None) else None
    key = (kind, salt, redis_url, getattr(settings, "session_key_prefix", None), settings.cosmos_endpoint,
           mem.cosmos_container or settings.cosmos_container, mem.retention_days, mem.max_items, mem.max_chars,
           mem.timeout_seconds, mem.cache_seconds, mem.writes_per_minute, settings.service_name,
           settings.environment, mem.dev_user)
    with _LOCK:
        service = _SERVICES.get(key)
        if service is None:
            service = MemoryService(
                _build_store(kind, settings, mem, credential),
                salt=salt,
                policy=MemoryPolicy(max_items=mem.max_items, max_chars=mem.max_chars, ttl_days=mem.retention_days),
                timeout_seconds=mem.timeout_seconds,
                cache_seconds=mem.cache_seconds,
                writes_per_minute=mem.writes_per_minute,
            )
            service.config = {  # what the console's Security page and Memory page show
                "store": kind, "retention_days": mem.retention_days, "max_items": mem.max_items,
                "max_chars": mem.max_chars, "salt_configured": configured,
                "dev_user": bool(mem.dev_user) and settings.environment in ("local", "test"),
            }
            service.dev_user = mem.dev_user if settings.environment in ("local", "test") else None
            _SERVICES[key] = service
        return service
