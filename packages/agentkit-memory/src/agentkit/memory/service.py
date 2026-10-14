"""Memory use cases: recall, remember, edit, delete, forget everything, export.

The service is the only thing that touches a store. It applies the policy, and it keeps memory from
ever taking the agent down:

* **timeouts** on every store call, and a **circuit breaker** that answers ``MemoryUnavailable`` at once
  while the store is failing, instead of making every request wait for it;
* **optimistic concurrency** with bounded retries, so writers on two replicas never lose each other's change;
* a short **read cache** per user (invalidated on every write through this process), so a conversation
  doesn't re-read the store on each turn;
* a **per-user rate limit** on writes;
* **keys derived with HMAC** from the tenant and user id: raw identities never reach the store.

Telemetry carries operation, outcome and item counts only, never memory text.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import random
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from opentelemetry import metrics, trace

from .errors import MemoryConflict, MemoryError_, MemoryRateLimited, MemoryUnavailable
from .models import MemoryDoc, MemoryItem, MemoryKind, MemorySource
from .policy import MemoryPolicy
from .stores import MemoryStore

__all__ = ["CircuitBreaker", "MemoryService", "MemoryUser"]

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("agentkit.memory")
_ops = metrics.get_meter("agentkit.memory").create_counter(
    "agentkit.memory.operations", unit="{operation}", description="Memory operations by kind and outcome")

T = TypeVar("T")
_WRITE_ATTEMPTS = 4


@dataclass(frozen=True)
class MemoryUser:
    """Whose memory. Both parts go into the key, so tenants never share a user's memory."""

    user_id: str
    tenant_id: str | None = None


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; after ``cooldown`` lets one trial call through."""

    def __init__(self, threshold: int = 5, cooldown: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._threshold, self._cooldown, self._clock = threshold, cooldown, clock
        self._failures = 0
        self._open_until = 0.0
        self._trial = False

    def allow(self) -> bool:
        if self._failures < self._threshold:
            return True
        if self._clock() >= self._open_until and not self._trial:
            self._trial = True  # half-open: exactly one call tests the store
            return True
        return False

    def retry_after(self) -> int:
        return max(1, int(self._open_until - self._clock()) + 1)

    def success(self) -> None:
        self._failures, self._trial = 0, False

    def failure(self) -> None:
        self._failures += 1
        self._trial = False
        if self._failures >= self._threshold:
            self._open_until = self._clock() + self._cooldown


class _Window:
    """Sliding-window counters per key, bounded in how many keys it remembers."""

    def __init__(self, limit: int, window: float = 60.0, max_keys: int = 10_000, clock=time.monotonic) -> None:
        self._limit, self._window, self._max, self._clock = limit, window, max_keys, clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def take(self, key: str) -> int:
        """0 when allowed (and counted), otherwise seconds until the next call may pass."""
        if self._limit <= 0:
            return 0
        now = self._clock()
        hits = self._hits.pop(key, None) or deque()
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        self._hits[key] = hits
        while len(self._hits) > self._max:
            self._hits.popitem(last=False)
        if len(hits) >= self._limit:
            return max(1, int(self._window - (now - hits[0])) + 1)
        hits.append(now)
        return 0


class MemoryService:
    def __init__(
        self,
        store: MemoryStore,
        *,
        salt: str,
        policy: MemoryPolicy | None = None,
        timeout_seconds: float = 2.0,
        cache_seconds: float = 15.0,
        writes_per_minute: int = 30,
        breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if len(salt) < 16:
            raise ValueError("the memory salt must be at least 16 characters")
        self.store = store
        self.policy = policy or MemoryPolicy()
        self._salt = salt.encode()
        self._timeout = timeout_seconds
        self._cache_seconds = cache_seconds
        self._cache: OrderedDict[str, tuple[float, tuple[MemoryItem, ...]]] = OrderedDict()
        self._cache_max = 2_048
        self._writes = _Window(writes_per_minute, clock=monotonic)
        self._breaker = breaker or CircuitBreaker(clock=monotonic)
        self._clock = clock
        self._monotonic = monotonic
        #: What the console shows about this memory (store, retention, limits); set by ``memory_service()``.
        self.config: dict[str, Any] = {"store": getattr(store, "kind", "custom"),
                                       "retention_days": self.policy.ttl_days, "max_items": self.policy.max_items,
                                       "max_chars": self.policy.max_chars, "salt_configured": True,
                                       "dev_user": False}
        #: Local and test only: whose memory to use when a request carries no identity.
        self.dev_user: str | None = None

    # ---------------------------------------------------------------------------- identity
    def key_for(self, user: MemoryUser) -> str:
        message = f"{user.tenant_id or ''}\x1f{user.user_id}".encode()
        return hmac.new(self._salt, message, hashlib.sha256).hexdigest()[:32]

    # --------------------------------------------------------------------------- use cases
    async def recall(self, user: MemoryUser) -> list[MemoryItem]:
        """What the agent remembers about ``user``, most recently updated first."""
        key = self.key_for(user)
        cached = self._cache.get(key)
        if cached and cached[0] > self._monotonic():
            self._cache.move_to_end(key)
            self._count("recall", "cache_hit")
            return list(cached[1])
        with _tracer.start_as_current_span("memory.recall") as span:
            doc, _ = await self._guarded("recall", lambda: self.store.load(key))
            items = sorted(self.policy.live(doc, self._clock()).items, key=lambda i: i.updated_at, reverse=True)
            span.set_attribute("memory.items", len(items))
        self._remember_in_cache(key, items)
        self._count("recall", "ok")
        return items

    async def remember(
        self,
        user: MemoryUser,
        text: str,
        *,
        kind: MemoryKind = "preference",
        source: MemorySource = "agent",
        idempotency_key: str | None = None,
    ) -> tuple[MemoryItem, bool]:
        """Store ``text`` for ``user``. Returns ``(item, created)``; see ``MemoryPolicy.add``."""
        clean = await self._screen("remember", text)
        self._rate_limit("remember", user)
        result: dict[str, Any] = {}

        def change(doc: MemoryDoc, now: float) -> MemoryDoc:
            new_doc, item, created = self.policy.add(doc, clean, kind=kind, source=source, now=now,
                                                     idempotency_key=idempotency_key)
            result.update(item=item, created=created)
            return new_doc

        await self._write("remember", user, change)
        return result["item"], result["created"]

    async def edit(self, user: MemoryUser, item_id: str, text: str) -> MemoryItem:
        clean = await self._screen("edit", text)
        self._rate_limit("edit", user)
        result: dict[str, MemoryItem] = {}

        def change(doc: MemoryDoc, now: float) -> MemoryDoc:
            new_doc, item = self.policy.edit(doc, item_id, clean, now=now)
            result["item"] = item
            return new_doc

        await self._write("edit", user, change)
        return result["item"]

    async def delete(self, user: MemoryUser, item_id: str) -> bool:
        """Remove one item. Idempotent: returns False when it was already gone."""
        result = {"removed": False}

        def change(doc: MemoryDoc, now: float) -> MemoryDoc:
            new_doc, removed = self.policy.remove(doc, item_id, now=now)
            result["removed"] = removed
            return new_doc

        await self._write("delete", user, change)
        return result["removed"]

    async def forget_all(self, user: MemoryUser) -> None:
        key = self.key_for(user)
        with _tracer.start_as_current_span("memory.forget_all"):
            await self._guarded("forget_all", lambda: self.store.delete(key))
        self._cache.pop(key, None)
        self._count("forget_all", "ok")

    async def export_markdown(self, user: MemoryUser, *, agent_name: str) -> str:
        items = await self.recall(user)
        lines = [f"# What {agent_name} remembers about you", ""]
        if not items:
            lines.append("_Nothing is remembered._")
        for item in items:
            stamp = time.strftime("%Y-%m-%d", time.gmtime(item.updated_at))
            until = time.strftime("%Y-%m-%d", time.gmtime(item.expires_at))
            lines.append(f"- {item.text}  \n  _{item.kind}, from {item.source}, updated {stamp}, kept until {until}_")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------------------ helpers
    async def _screen(self, op: str, text: str) -> str:
        try:
            return await self.policy.clean(text)
        except MemoryError_ as exc:
            self._count(op, exc.code)
            raise

    def _rate_limit(self, op: str, user: MemoryUser) -> None:
        wait = self._writes.take(self.key_for(user))
        if wait:
            self._count(op, "rate_limited")
            raise MemoryRateLimited("Too many memory changes; try again shortly.", retry_after=wait)

    async def _write(self, op: str, user: MemoryUser, change: Callable[[MemoryDoc, float], MemoryDoc]) -> None:
        key = self.key_for(user)
        with _tracer.start_as_current_span(f"memory.{op}") as span:
            for attempt in range(1, _WRITE_ATTEMPTS + 1):
                doc, token = await self._guarded(op, lambda: self.store.load(key))
                now = self._clock()
                new_doc = change(doc, now)
                if new_doc is doc:  # nothing changed (e.g. deleting what isn't there)
                    break
                try:
                    await self._guarded(op, lambda: self.store.save(key, new_doc, token, self.policy.expiry(new_doc)))
                    break
                except MemoryConflict:
                    span.add_event("memory.conflict", {"attempt": attempt})
                    if attempt == _WRITE_ATTEMPTS:
                        self._count(op, "conflict")
                        raise
                    await asyncio.sleep(random.uniform(0.01, 0.05) * attempt)
            span.set_attribute("memory.attempts", attempt)
        self._cache.pop(key, None)
        self._count(op, "ok")

    async def _guarded(self, op: str, call: Callable[[], Awaitable[T]]) -> T:
        if not self._breaker.allow():
            self._count(op, "circuit_open")
            raise MemoryUnavailable("Memory is temporarily unavailable.", retry_after=self._breaker.retry_after())
        try:
            result = await asyncio.wait_for(call(), self._timeout)
        except MemoryConflict:
            self._breaker.success()  # the store answered; someone else simply wrote first
            raise
        except (asyncio.TimeoutError, OSError, ConnectionError) as exc:
            self._breaker.failure()
            self._count(op, "unavailable")
            logger.warning("memory %s failed: %s", op, type(exc).__name__)
            raise MemoryUnavailable("Memory is temporarily unavailable.") from exc
        except Exception as exc:  # store SDK errors (redis.ConnectionError, CosmosHttpResponseError, ...)
            if isinstance(exc, MemoryError_):
                raise
            self._breaker.failure()
            self._count(op, "unavailable")
            logger.warning("memory %s failed: %s", op, type(exc).__name__)
            raise MemoryUnavailable("Memory is temporarily unavailable.") from exc
        self._breaker.success()
        return result

    def _remember_in_cache(self, key: str, items: list[MemoryItem]) -> None:
        if self._cache_seconds <= 0:
            return
        self._cache[key] = (self._monotonic() + self._cache_seconds, tuple(items))
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)

    @staticmethod
    def _count(op: str, outcome: str) -> None:
        _ops.add(1, {"memory.op": op, "memory.outcome": outcome})

