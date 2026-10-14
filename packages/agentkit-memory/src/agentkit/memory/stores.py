"""Where memory documents live. One small interface, three backends, optimistic concurrency everywhere.

* ``InMemoryMemoryStore``: one process (tests, local dev). Lost on restart, so never for real users.
* ``RedisMemoryStore``: any Redis 6+. Uses only GET, SET, DEL and EVAL on its own key prefix, which is
  exactly what the VPS stack's per-agent Redis users may run (deploy/vps/agentctl.py).
* ``CosmosMemoryStore``: Azure Cosmos DB (NoSQL API), ETag-conditional writes, managed identity.

``load`` returns the document and a ``token``; ``save`` writes only if nobody wrote since that token,
otherwise it raises ``MemoryConflict`` and the service retries. A document with no items is deleted,
and every stored document expires on its own when its latest item does.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any, Protocol

from .errors import MemoryConflict
from .models import MemoryDoc

__all__ = ["CosmosMemoryStore", "InMemoryMemoryStore", "MemoryStore", "RedisMemoryStore"]


class MemoryStore(Protocol):
    kind: str

    async def load(self, user_key: str) -> tuple[MemoryDoc, Any]:
        """The user's document (empty when there is none) and a concurrency token for ``save``."""
        ...

    async def save(self, user_key: str, doc: MemoryDoc, token: Any, expires_at: float | None) -> None:
        """Write ``doc`` if unchanged since ``token``; delete it when ``expires_at`` is None (no items)."""
        ...

    async def delete(self, user_key: str) -> None:
        """Remove everything for the user, unconditionally (forget me)."""
        ...


def _ttl(expires_at: float, now: float) -> int:
    return max(1, math.ceil(expires_at - now))


# ---------------------------------------------------------------------------------- in-memory
class InMemoryMemoryStore:
    kind = "memory"

    def __init__(self, *, clock=time.time) -> None:
        self._docs: dict[str, tuple[str, float]] = {}  # key -> (json, expires_at)
        self._lock = asyncio.Lock()
        self._clock = clock

    async def load(self, user_key: str) -> tuple[MemoryDoc, Any]:
        async with self._lock:
            entry = self._docs.get(user_key)
            if entry is None or entry[1] <= self._clock():
                self._docs.pop(user_key, None)
                return MemoryDoc(), 0
            doc = MemoryDoc.model_validate_json(entry[0])
            return doc, doc.version

    async def save(self, user_key: str, doc: MemoryDoc, token: Any, expires_at: float | None) -> None:
        async with self._lock:
            entry = self._docs.get(user_key)
            alive = entry is not None and entry[1] > self._clock()
            current = MemoryDoc.model_validate_json(entry[0]).version if alive else 0
            if current != token:
                raise MemoryConflict("The memory changed while it was being updated.")
            if expires_at is None:
                self._docs.pop(user_key, None)
                return
            stored = doc.model_copy(update={"version": current + 1})
            self._docs[user_key] = (stored.model_dump_json(), expires_at)

    async def delete(self, user_key: str) -> None:
        async with self._lock:
            self._docs.pop(user_key, None)


# -------------------------------------------------------------------------------------- redis
# Compare-and-set in one round trip. KEYS[1] = document key. ARGV = expected version, new JSON (or ""),
# TTL seconds. Only GET, SET and DEL run inside, so it works under a Redis ACL that allows just
# +get +set +del +eval on the agent's own prefix.
_CAS_LUA = """
local current = redis.call('GET', KEYS[1])
local version = 0
if current then
  local ok, decoded = pcall(cjson.decode, current)
  if ok and type(decoded) == 'table' and tonumber(decoded['version']) then
    version = tonumber(decoded['version'])
  end
end
if version ~= tonumber(ARGV[1]) then
  return 0
end
if ARGV[2] == '' then
  redis.call('DEL', KEYS[1])
else
  redis.call('SET', KEYS[1], ARGV[2], 'EX', tonumber(ARGV[3]))
end
return 1
"""


class RedisMemoryStore:
    kind = "redis"

    def __init__(self, client: Any, *, prefix: str = "agentkit:", clock=time.time) -> None:
        self._r = client
        self._prefix = prefix
        self._clock = clock

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisMemoryStore:
        from redis.asyncio import Redis

        return cls(Redis.from_url(url, decode_responses=True), **kwargs)

    def _key(self, user_key: str) -> str:
        return f"{self._prefix}memory:{user_key}"

    async def load(self, user_key: str) -> tuple[MemoryDoc, Any]:
        raw = await self._r.get(self._key(user_key))
        if not raw:
            return MemoryDoc(), 0
        doc = MemoryDoc.model_validate_json(raw)
        return doc, doc.version

    async def save(self, user_key: str, doc: MemoryDoc, token: Any, expires_at: float | None) -> None:
        if expires_at is None:
            payload, ttl = "", 0
        else:
            payload = doc.model_copy(update={"version": int(token) + 1}).model_dump_json()
            ttl = _ttl(expires_at, self._clock())
        ok = await self._r.eval(_CAS_LUA, 1, self._key(user_key), int(token), payload, ttl)
        if int(ok) != 1:
            raise MemoryConflict("The memory changed while it was being updated.")

    async def delete(self, user_key: str) -> None:
        await self._r.delete(self._key(user_key))

    async def aclose(self) -> None:
        close = getattr(self._r, "aclose", None) or getattr(self._r, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


# ------------------------------------------------------------------------------------- cosmos
class CosmosMemoryStore:
    """Cosmos DB (NoSQL API). Works in the session container (partition key ``/id``, default TTL on):
    memory documents use ids ``memory:<user key>`` and never collide with session ids."""

    kind = "cosmos"

    def __init__(self, container: Any, *, clock=time.time) -> None:
        self._c = container
        self._clock = clock

    @staticmethod
    def _id(user_key: str) -> str:
        return f"memory:{user_key}"

    async def load(self, user_key: str) -> tuple[MemoryDoc, Any]:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            item = await self._c.read_item(item=self._id(user_key), partition_key=self._id(user_key))
        except CosmosResourceNotFoundError:
            return MemoryDoc(), None
        if float(item.get("expires_at", 0)) <= self._clock():
            return MemoryDoc(), item.get("_etag")
        return MemoryDoc.model_validate(item["doc"]), item.get("_etag")

    async def save(self, user_key: str, doc: MemoryDoc, token: Any, expires_at: float | None) -> None:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceExistsError,
            CosmosResourceNotFoundError,
        )

        item_id = self._id(user_key)
        try:
            if expires_at is None:
                if token is not None:
                    await self._c.delete_item(item=item_id, partition_key=item_id, etag=token,
                                              match_condition=MatchConditions.IfNotModified)
                return
            body = {
                "id": item_id,
                "doc": json.loads(doc.model_copy(update={"version": doc.version + 1}).model_dump_json()),
                "expires_at": expires_at,
                "ttl": _ttl(expires_at, self._clock()),
            }
            if token is None:
                await self._c.create_item(body)
            else:
                await self._c.replace_item(item=item_id, body=body, etag=token,
                                           match_condition=MatchConditions.IfNotModified)
        except (CosmosAccessConditionFailedError, CosmosResourceExistsError) as exc:
            raise MemoryConflict("The memory changed while it was being updated.") from exc
        except CosmosResourceNotFoundError as exc:
            if expires_at is None:
                return  # already gone: the outcome the caller wanted
            raise MemoryConflict("The memory was removed while it was being updated.") from exc

    async def delete(self, user_key: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            await self._c.delete_item(item=self._id(user_key), partition_key=self._id(user_key))
        except CosmosResourceNotFoundError:
            pass
