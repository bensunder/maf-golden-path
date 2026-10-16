"""Governed long-term memory for agentkit services (docs/memory.md).

Four kinds, on by default for every MAF agent built with ``build_agent`` (``AGENTKIT_MEMORY``):

* **user memory**: each signed-in person's stated preferences (``UserMemoryProvider``);
* **lessons**: the agent's own reviewed notebook; **subject memory**: per-customer/invoice history that works
  without a signed-in person; **run journal**: what each run did (``AgentMemoryProvider``, ``JournalMiddleware``).

``build_agent`` and ``create_app`` wire it all; by hand it is::

    from agentkit.memory import AgentMemoryProvider, JournalMiddleware, MemoryChannel, UserMemoryProvider
    from agentkit.memory import agent_memory, memory_service

    am = agent_memory(settings)                              # one per process, shared
    agent = build_agent(..., context_providers=[UserMemoryProvider(am.memory), AgentMemoryProvider(am)],
                        extra_middleware=[JournalMiddleware(am.journal)])
    app = create_app(create_agent, channels=[MemoryChannel(), ...])
"""

from .agent_memory import (
    AGENT_MEMORY_TOOLS,
    FORGET_LESSON_TOOL,
    HISTORY_TOOL,
    LOG_EVENT_TOOL,
    RECENT_TOOL,
    REMEMBER_LESSON_TOOL,
    AgentMemory,
    AgentMemoryProvider,
)
from .api import MemoryChannel
from .errors import MemoryConflict, MemoryError_, MemoryNotFound, MemoryRateLimited, MemoryRejected, MemoryUnavailable
from .journal import JournalMiddleware, RunJournal, RunLedger, current_ledger, run_trigger
from .lessons import LessonBook, LessonPolicy
from .models import (
    JournalEntry,
    Lesson,
    MemoryDoc,
    MemoryItem,
    MemoryItemIn,
    MemoryItemOut,
    MemoryItemPatch,
    MemoryListOut,
    SubjectEvent,
)
from .policy import MemoryPolicy
from .provider import FORGET_TOOL, REMEMBER_TOOL, UserMemoryProvider
from .service import CircuitBreaker, MemoryService, MemoryUser
from .settings import MemorySettings, agent_memory, memory_service, reset_memory_services
from .stores import CosmosMemoryStore, InMemoryMemoryStore, MemoryStore, RedisMemoryStore
from .subjects import SubjectMemory, SubjectPolicy, check_subject

__all__ = [
    "AGENT_MEMORY_TOOLS",
    "FORGET_LESSON_TOOL",
    "FORGET_TOOL",
    "HISTORY_TOOL",
    "LOG_EVENT_TOOL",
    "RECENT_TOOL",
    "REMEMBER_LESSON_TOOL",
    "AgentMemory",
    "AgentMemoryProvider",
    "JournalEntry",
    "JournalMiddleware",
    "Lesson",
    "LessonBook",
    "LessonPolicy",
    "RunJournal",
    "RunLedger",
    "SubjectEvent",
    "SubjectMemory",
    "SubjectPolicy",
    "agent_memory",
    "check_subject",
    "current_ledger",
    "run_trigger",
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
