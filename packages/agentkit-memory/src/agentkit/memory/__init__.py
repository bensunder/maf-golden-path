"""Governed long-term user memory for agentkit services (docs/memory.md).

    from agentkit.memory import MemoryChannel, UserMemoryProvider, memory_service

    memory = memory_service(settings)                       # one per process, shared
    agent = build_agent(..., context_providers=[UserMemoryProvider(memory)])
    app = create_app(create_agent, channels=[MemoryChannel(memory), ...])
"""

from .api import MemoryChannel
from .errors import MemoryConflict, MemoryError_, MemoryNotFound, MemoryRateLimited, MemoryRejected, MemoryUnavailable
from .models import MemoryDoc, MemoryItem, MemoryItemIn, MemoryItemOut, MemoryItemPatch, MemoryListOut
from .policy import MemoryPolicy
from .provider import FORGET_TOOL, REMEMBER_TOOL, UserMemoryProvider
from .service import CircuitBreaker, MemoryService, MemoryUser
from .settings import MemorySettings, memory_service, reset_memory_services
from .stores import CosmosMemoryStore, InMemoryMemoryStore, MemoryStore, RedisMemoryStore

__all__ = [
    "FORGET_TOOL",
    "REMEMBER_TOOL",
    "CircuitBreaker",
    "CosmosMemoryStore",
    "InMemoryMemoryStore",
    "MemoryChannel",
    "MemoryConflict",
    "MemoryDoc",
    "MemoryError_",
    "MemoryItem",
    "MemoryItemIn",
    "MemoryItemOut",
    "MemoryItemPatch",
    "MemoryListOut",
    "MemoryNotFound",
    "MemoryPolicy",
    "MemoryRateLimited",
    "MemoryRejected",
    "MemoryService",
    "MemorySettings",
    "MemoryStore",
    "MemoryUnavailable",
    "MemoryUser",
    "RedisMemoryStore",
    "UserMemoryProvider",
    "memory_service",
    "reset_memory_services",
]
