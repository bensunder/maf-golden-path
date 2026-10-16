"""Memory settings (``AGENTKIT_MEMORY_*``) and the one place memory services are built from them.

The agent's providers (``UserMemoryProvider``, ``AgentMemoryProvider``) and the HTTP API (``MemoryChannel``)
must share one ``MemoryService`` so a write through either invalidates the other's cache.
``memory_service()`` and ``agent_memory()`` return the same instance for the same configuration in a process.
"""

from __future__ import annotations

import threading
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .agent_memory import AgentMemory
from .journal import RunJournal
from .lessons import LessonBook, LessonPolicy
from .policy import MemoryPolicy
from .service import MemoryService
from .stores import CosmosMemoryStore, InMemoryMemoryStore, MemoryStore, RedisMemoryStore
from .subjects import SubjectMemory, SubjectPolicy

__all__ = ["MemorySettings", "agent_memory", "memory_service", "reset_memory_services"]


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

    # ---- agent memory: lessons, subjects, journal (docs/memory.md)
    #: Comma-separated user ids (as the platform's user header carries them, e.g. e-mail addresses) who review
    #: the agent's lessons and may read its subject memory and journal. Empty: lessons wait until one is set.
    admins: str = ""
    max_lessons: int = Field(default=100, ge=1, le=500)
    lesson_retention_days: int = Field(default=365, ge=1, le=3650)
    #: How long a lesson waits for review, and how long an archived one can be restored.
    lesson_review_days: int = Field(default=30, ge=1, le=365)
    subject_retention_days: int = Field(default=365, ge=1, le=3650)
    max_subject_events: int = Field(default=200, ge=1, le=2000)
    journal_retention_days: int = Field(default=90, ge=1, le=3650)

    def admin_ids(self) -> frozenset[str]:
        return frozenset(a.strip().casefold() for a in self.admins.split(",") if a.strip())


_LOCK = threading.RLock()
_SERVICES: dict[tuple[Any, ...], MemoryService] = {}
_AGENT_MEMORIES: dict[tuple[Any, ...], AgentMemory] = {}


def reset_memory_services() -> None:
    """Forget built services (tests)."""
    with _LOCK:
        _SERVICES.clear()
        _AGENT_MEMORIES.clear()


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


def agent_memory(settings: Any = None, memory_settings: MemorySettings | None = None, *,
                 credential: Any = None) -> AgentMemory:
    """The process's ``AgentMemory`` (lessons, subjects, journal) for this configuration, sharing ``memory_service``."""
    if settings is None:
        from agentkit.hosting import AgentKitSettings

        settings = AgentKitSettings()
    mem = memory_settings or MemorySettings()
    service = memory_service(settings, mem, credential=credential)
    key = (id(service), settings.service_name, mem.admins, mem.max_lessons, mem.lesson_retention_days,
           mem.lesson_review_days, mem.subject_retention_days, mem.max_subject_events, mem.journal_retention_days)
    with _LOCK:
        built = _AGENT_MEMORIES.get(key)
        if built is None:
            agent = settings.service_name
            built = AgentMemory(
                memory=service,
                lessons=LessonBook(service, agent=agent, policy=LessonPolicy(
                    max_active=mem.max_lessons, retention_days=mem.lesson_retention_days,
                    review_days=mem.lesson_review_days)),
                subjects=SubjectMemory(service, agent=agent, policy=SubjectPolicy(
                    max_events=mem.max_subject_events, retention_days=mem.subject_retention_days)),
                journal=RunJournal(service, agent=agent, retention_days=mem.journal_retention_days),
                admins=mem.admin_ids(),
                config={"lesson_retention_days": mem.lesson_retention_days, "max_lessons": mem.max_lessons,
                        "lesson_review_days": mem.lesson_review_days,
                        "subject_retention_days": mem.subject_retention_days,
                        "max_subject_events": mem.max_subject_events,
                        "journal_retention_days": mem.journal_retention_days},
            )
            _AGENT_MEMORIES[key] = built
        return built
