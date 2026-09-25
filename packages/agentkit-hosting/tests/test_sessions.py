"""One contract, three backends. Two store instances over one backend simulate two replicas."""

import asyncio
import time

import pytest
from azure.cosmos.exceptions import CosmosResourceExistsError, CosmosResourceNotFoundError

from agentkit.hosting import AgentKitSettings
from agentkit.hosting.sessions import (
    CosmosSessionStore,
    InMemorySessionStore,
    RedisSessionStore,
    SessionLockTimeout,
    SessionRecord,
    session_store_from_settings,
)

# ----------------------------------------------------------------------------- backends


class FakeCosmosContainer:
    """Mimics azure.cosmos.aio.ContainerProxy semantics used by the store (404/409, per-item ttl)."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def _alive(self, item_id: str) -> dict | None:
        item = self.items.get(item_id)
        if item and item.get("ttl") and item["_created"] + item["ttl"] <= time.time():
            del self.items[item_id]
            return None
        return item

    async def read_item(self, item, partition_key):
        assert item == partition_key  # partition key is /id
        found = self._alive(item)
        if found is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return {k: v for k, v in found.items() if k != "_created"}

    async def upsert_item(self, body):
        self.items[body["id"]] = {**body, "_created": time.time()}
        return body

    async def create_item(self, body):
        if self._alive(body["id"]) is not None:
            raise CosmosResourceExistsError(status_code=409, message="conflict")
        self.items[body["id"]] = {**body, "_created": time.time()}
        return body

    async def delete_item(self, item, partition_key):
        if self.items.pop(item, None) is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")


@pytest.fixture(params=["memory", "redis", "cosmos"])
async def replicas(request):
    """Two store instances that share one backend, like two replicas of a service."""
    kind = request.param
    if kind == "memory":
        store = InMemorySessionStore(lock_wait_seconds=0.3)
        yield store, store  # one process: both "replicas" are the same object
    elif kind == "redis":
        url = request.getfixturevalue("redis_url")
        a = RedisSessionStore.from_url(url, prefix=f"t{time.time_ns()}:", lock_wait_seconds=0.3)
        b = RedisSessionStore(a._r, prefix=a._prefix, lock_wait_seconds=0.3)
        yield a, b
        await a._r.aclose()
    else:
        container = FakeCosmosContainer()
        yield CosmosSessionStore(container, lock_wait_seconds=0.3), CosmosSessionStore(container, lock_wait_seconds=0.3)


def record(owner="ben", ttl=60, **meta):
    return SessionRecord(owner=owner, data={"type": "session", "session_id": "s", "state": {"k": [1, 2]}},
                         expires_at=time.time() + ttl, meta=meta)


# ----------------------------------------------------------------------------- contract


async def test_roundtrip_is_visible_to_other_replica(replicas):
    a, b = replicas
    await a.put("s1", record(approvals=[{"id": "x"}]))
    got = await b.get("s1")
    assert got.owner == "ben"
    assert got.data["state"] == {"k": [1, 2]}
    assert got.meta == {"approvals": [{"id": "x"}]}
    await b.delete("s1")
    assert await a.get("s1") is None
    await a.delete("never-existed")  # idempotent


async def test_expired_sessions_are_gone(replicas):
    a, b = replicas
    await a.put("old", SessionRecord(owner=None, data={}, expires_at=time.time() - 1))
    assert await b.get("old") is None


async def test_lock_serializes_replicas(replicas):
    a, b = replicas
    order = []

    async def work(store, tag):
        async with store.lock("s2"):
            order.append(f"{tag}-in")
            await asyncio.sleep(0.05)
            order.append(f"{tag}-out")

    await asyncio.gather(work(a, "a"), work(b, "b"))
    assert order[0].endswith("-in") and order[1].endswith("-out") and order[0][0] == order[1][0]


async def test_lock_times_out_while_held(replicas):
    a, b = replicas
    async with a.lock("s3"):
        with pytest.raises(SessionLockTimeout):
            async with b.lock("s3"):
                pass
    async with b.lock("s3"):  # released afterwards
        pass


async def test_lock_released_on_error(replicas):
    a, b = replicas
    with pytest.raises(RuntimeError):
        async with a.lock("s4"):
            raise RuntimeError("boom")
    async with b.lock("s4"):
        pass


async def test_different_sessions_do_not_block(replicas):
    a, b = replicas
    async with a.lock("x1"):
        async with b.lock("x2"):
            pass


# ----------------------------------------------------------------------------- backend specifics


async def test_redis_ttl_is_set(redis_url):
    store = RedisSessionStore.from_url(redis_url, prefix=f"ttl{time.time_ns()}:")
    await store.put("s", record(ttl=100))
    ttl = await store._r.ttl(store._key("s"))
    assert 90 <= ttl <= 100
    await store._r.aclose()


async def test_redis_lock_expires_if_holder_dies(redis_url):
    a = RedisSessionStore.from_url(redis_url, prefix=f"die{time.time_ns()}:", lock_ttl_seconds=0.2, lock_wait_seconds=1)
    await a._r.set(f"{a._prefix}lock:s", "crashed-replica-token", px=200)  # a lock nobody will release
    async with a.lock("s"):  # acquired once the orphaned lock expires
        pass
    await a._r.aclose()


async def test_cosmos_item_shape_and_stale_lease():
    container = FakeCosmosContainer()
    store = CosmosSessionStore(container, lock_wait_seconds=1)
    await store.put("s", record(ttl=100))
    item = container.items["s"]
    assert item["id"] == "s" and 90 <= item["ttl"] <= 100 and item["owner"] == "ben"
    container.items["lock:s"] = {"id": "lock:s", "token": "dead", "ttl": 0, "expires_at": time.time() - 5,
                                 "_created": time.time()}  # TTL deletion lagging behind
    async with store.lock("s"):
        assert container.items["lock:s"]["token"] != "dead"
    assert "lock:s" not in container.items


def test_factory_and_validation():
    assert isinstance(session_store_from_settings(AgentKitSettings(_env_file=None)), InMemorySessionStore)
    with pytest.raises(ValueError, match="redis_url"):
        AgentKitSettings(session_store="redis", _env_file=None)
    with pytest.raises(ValueError, match="cosmos_endpoint"):
        AgentKitSettings(session_store="cosmos", _env_file=None)
    s = AgentKitSettings(session_store="redis", redis_url="redis://localhost:1/0", _env_file=None)
    assert isinstance(session_store_from_settings(s), RedisSessionStore)
    c = AgentKitSettings(session_store="cosmos", cosmos_endpoint="https://x.documents.azure.com:443/",
                         cosmos_container="orders-sessions", auth_mode="api_key", api_key="k", _env_file=None)

    class Cred:  # the Cosmos client is created lazily; no network here
        async def get_token(self, *a, **k): ...

    assert isinstance(session_store_from_settings(c, credential=Cred()), CosmosSessionStore)
