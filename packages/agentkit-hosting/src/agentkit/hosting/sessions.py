"""Session persistence and per-session locking behind one small interface.

* ``InMemorySessionStore``: one replica (tests, local dev).
* ``RedisSessionStore``: Azure Cache for Redis / any Redis 6+; lowest latency.
* ``CosmosSessionStore``: Azure Cosmos DB with managed identity (no secrets); the Azure default.

All stores hold the serialized MAF ``AgentSession`` plus agentkit metadata (owner, pending
approvals, audit log), and provide ``lock(session_id)`` so two replicas never run the same
conversation at the same time.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

__all__ = [
    "CosmosSessionStore",
    "InMemorySessionStore",
    "RedisSessionStore",
    "SessionLockTimeout",
    "SessionRecord",
    "SessionStore",
    "session_store_from_settings",
]


class SessionLockTimeout(Exception):
    """Another request (possibly on another replica) is still working on this session."""


@dataclass
class SessionRecord:
    owner: str | None
    data: dict[str, Any]  # AgentSession.to_dict()
    expires_at: float  # epoch seconds
    meta: dict[str, Any] = field(default_factory=dict)  # pending approvals, audit log, ...

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> SessionRecord:
        return cls(**json.loads(raw))

    def ttl_seconds(self, now: float | None = None) -> int:
        return max(1, int(self.expires_at - (now if now is not None else time.time())))


class SessionStore(Protocol):
    async def get(self, session_id: str) -> SessionRecord | None: ...

    async def put(self, session_id: str, record: SessionRecord) -> None: ...

    async def delete(self, session_id: str) -> None: ...

    def lock(self, session_id: str) -> Any:
        """Async context manager; raises SessionLockTimeout if not acquired in time."""
        ...


# ------------------------------------------------------------------------------------ in-memory
class _KeyedLocks:
    """One asyncio lock per key while in use; entries are dropped when the last holder leaves."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._holders: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._locks)

    @asynccontextmanager
    async def hold(self, key: str, timeout: float | None = None) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._holders[key] = self._holders.get(key, 0) + 1
        try:
            try:
                await asyncio.wait_for(lock.acquire(), timeout)
            except asyncio.TimeoutError as exc:
                raise SessionLockTimeout(key) from exc
            try:
                yield
            finally:
                lock.release()
        finally:
            self._holders[key] -= 1
            if self._holders[key] == 0:
                del self._holders[key]
                del self._locks[key]


class InMemorySessionStore:
    """TTL + LRU bounded, single replica. The generated infra caps replicas at 1 with this store."""

    def __init__(self, *, max_sessions: int = 10_000, lock_wait_seconds: float = 30.0, clock=time.time) -> None:
        self._data: OrderedDict[str, SessionRecord] = OrderedDict()
        self._max = max_sessions
        self._clock = clock
        self._guard = asyncio.Lock()
        self._locks = _KeyedLocks()
        self._lock_wait = lock_wait_seconds

    async def get(self, session_id: str) -> SessionRecord | None:
        async with self._guard:
            record = self._data.get(session_id)
            if record is None:
                return None
            if record.expires_at <= self._clock():
                del self._data[session_id]
                return None
            self._data.move_to_end(session_id)
            return record

    async def put(self, session_id: str, record: SessionRecord) -> None:
        async with self._guard:
            self._data[session_id] = record
            self._data.move_to_end(session_id)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    async def delete(self, session_id: str) -> None:
        async with self._guard:
            self._data.pop(session_id, None)

    def lock(self, session_id: str):
        return self._locks.hold(session_id, self._lock_wait)

    @property
    def active_locks(self) -> int:
        return len(self._locks)


# ------------------------------------------------------------------------------------ redis
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""


class RedisSessionStore:
    """Redis-backed sessions with a token-checked distributed lock (SET NX PX + compare-and-delete)."""

    def __init__(
        self,
        client: Any,
        *,
        prefix: str = "agentkit:",
        lock_ttl_seconds: float = 120.0,
        lock_wait_seconds: float = 30.0,
    ) -> None:
        self._r = client
        self._prefix = prefix
        self._lock_ttl_ms = int(lock_ttl_seconds * 1000)
        self._lock_wait = lock_wait_seconds

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisSessionStore:
        from redis.asyncio import Redis

        return cls(Redis.from_url(url, decode_responses=True), **kwargs)

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}session:{session_id}"

    async def get(self, session_id: str) -> SessionRecord | None:
        raw = await self._r.get(self._key(session_id))
        if not raw:
            return None
        record = SessionRecord.from_json(raw)
        return record if record.expires_at > time.time() else None

    async def put(self, session_id: str, record: SessionRecord) -> None:
        if record.expires_at <= time.time():
            await self.delete(session_id)
            return
        await self._r.set(self._key(session_id), record.to_json(), ex=record.ttl_seconds())

    async def delete(self, session_id: str) -> None:
        await self._r.delete(self._key(session_id))

    @asynccontextmanager
    async def lock(self, session_id: str) -> AsyncIterator[None]:
        key, token = f"{self._prefix}lock:{session_id}", uuid.uuid4().hex
        deadline = time.monotonic() + self._lock_wait
        delay = 0.02
        while not await self._r.set(key, token, nx=True, px=self._lock_ttl_ms):
            if time.monotonic() >= deadline:
                raise SessionLockTimeout(session_id)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.5)
        try:
            yield
        finally:
            await self._r.eval(_RELEASE_LUA, 1, key, token)


# ------------------------------------------------------------------------------------ cosmos
class CosmosSessionStore:
    """Cosmos DB (NoSQL API) sessions. Container: partition key ``/id``, default TTL enabled (-1).

    Uses per-item ``ttl`` for expiry and short-lived lease documents for locking.
    Authenticate with the service's managed identity ("Cosmos DB Built-in Data Contributor").
    """

    def __init__(self, container: Any, *, lock_ttl_seconds: int = 120, lock_wait_seconds: float = 30.0) -> None:
        self._c = container
        self._lock_ttl = int(lock_ttl_seconds)
        self._lock_wait = lock_wait_seconds

    async def get(self, session_id: str) -> SessionRecord | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            item = await self._c.read_item(item=session_id, partition_key=session_id)
        except CosmosResourceNotFoundError:
            return None
        if item.get("expires_at", 0) <= time.time():
            return None
        return SessionRecord(owner=item.get("owner"), data=item["data"], expires_at=item["expires_at"], meta=item.get("meta") or {})

    async def put(self, session_id: str, record: SessionRecord) -> None:
        await self._c.upsert_item(
            {
                "id": session_id,
                "owner": record.owner,
                "data": json.loads(json.dumps(record.data, default=str)),
                "meta": json.loads(json.dumps(record.meta, default=str)),
                "expires_at": record.expires_at,
                "ttl": record.ttl_seconds(),
            }
        )

    async def delete(self, session_id: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            await self._c.delete_item(item=session_id, partition_key=session_id)
        except CosmosResourceNotFoundError:
            pass

    @asynccontextmanager
    async def lock(self, session_id: str) -> AsyncIterator[None]:
        from azure.cosmos.exceptions import CosmosResourceExistsError, CosmosResourceNotFoundError

        lease_id, token = f"lock:{session_id}", uuid.uuid4().hex
        deadline = time.monotonic() + self._lock_wait
        delay = 0.05
        while True:
            try:
                await self._c.create_item({"id": lease_id, "token": token, "ttl": self._lock_ttl,
                                           "expires_at": time.time() + self._lock_ttl})
                break
            except CosmosResourceExistsError:
                # Clear a lease left by a crashed replica once it has expired (TTL deletion can lag).
                try:
                    lease = await self._c.read_item(item=lease_id, partition_key=lease_id)
                    if lease.get("expires_at", 0) <= time.time():
                        await self._c.delete_item(item=lease_id, partition_key=lease_id)
                        continue
                except CosmosResourceNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise SessionLockTimeout(session_id) from None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)
        try:
            yield
        finally:
            try:
                lease = await self._c.read_item(item=lease_id, partition_key=lease_id)
                if lease.get("token") == token:
                    await self._c.delete_item(item=lease_id, partition_key=lease_id)
            except CosmosResourceNotFoundError:
                pass


# ------------------------------------------------------------------------------------ factory
def session_store_from_settings(settings: Any, credential: Any = None) -> SessionStore:
    """Build the store named by ``AGENTKIT_SESSION_STORE`` (memory | redis | cosmos)."""
    kind = settings.session_store
    if kind == "memory":
        return InMemorySessionStore()
    if kind == "redis":
        return RedisSessionStore.from_url(settings.redis_url.get_secret_value())
    if kind == "cosmos":
        from azure.cosmos.aio import CosmosClient

        if credential is None:
            from .clients import get_credential

            credential = get_credential(settings)
        client = CosmosClient(settings.cosmos_endpoint, credential=credential)
        container = client.get_database_client(settings.cosmos_database).get_container_client(settings.cosmos_container)
        return CosmosSessionStore(container)
    raise ValueError(f"unknown session store: {kind}")
