"""Session persistence behind a small interface (swap in Redis/Cosmos without touching agents)."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = ["InMemorySessionStore", "SessionRecord", "SessionStore"]


@dataclass
class SessionRecord:
    owner: str | None
    data: dict[str, Any]  # AgentSession.to_dict()
    expires_at: float


class SessionStore(Protocol):
    async def get(self, session_id: str) -> SessionRecord | None: ...

    async def put(self, session_id: str, record: SessionRecord) -> None: ...

    async def delete(self, session_id: str) -> None: ...


class InMemorySessionStore:
    """TTL + LRU bounded store for a single replica. Use a shared store when scaling out."""

    def __init__(self, *, max_sessions: int = 10_000, clock=time.monotonic) -> None:
        self._data: OrderedDict[str, SessionRecord] = OrderedDict()
        self._max = max_sessions
        self._clock = clock
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> SessionRecord | None:
        async with self._lock:
            record = self._data.get(session_id)
            if record is None:
                return None
            if record.expires_at <= self._clock():
                del self._data[session_id]
                return None
            self._data.move_to_end(session_id)
            return record

    async def put(self, session_id: str, record: SessionRecord) -> None:
        async with self._lock:
            self._data[session_id] = record
            self._data.move_to_end(session_id)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._data.pop(session_id, None)

    def now(self) -> float:
        return self._clock()
