"""One contract, four backends: in-memory, Redis, Redis under the VPS per-agent ACL, and Cosmos.
Two store instances over one backend simulate two replicas."""

import asyncio
import time

import pytest

from agentkit.memory import (
    CosmosMemoryStore,
    InMemoryMemoryStore,
    MemoryConflict,
    MemoryDoc,
    MemoryItem,
    RedisMemoryStore,
)


def _doc(*texts: str, ttl: float = 3600) -> MemoryDoc:
    now = time.time()
    items = [MemoryItem(id=f"mem_{n:012x}", text=t, created_at=now, updated_at=now, expires_at=now + ttl)
             for n, t in enumerate(texts)]
    return MemoryDoc(items=items, updated_at=now)


def _expiry(doc: MemoryDoc) -> float:
    return max(i.expires_at for i in doc.items)


@pytest.fixture(params=["memory", "redis", "redis-acl", "cosmos"])
async def replicas(request):
    """Two stores over one backend."""
    kind = request.param
    if kind == "memory":
        one = InMemoryMemoryStore()
        yield one, one
    elif kind == "redis":
        url = request.getfixturevalue("redis_url")
        prefix = f"t{time.time_ns()}:"
        a, b = RedisMemoryStore.from_url(url, prefix=prefix), RedisMemoryStore.from_url(url, prefix=prefix)
        yield a, b
        await a.aclose(), await b.aclose()
    elif kind == "redis-acl":
        url, prefix = request.getfixturevalue("restricted_redis")
        a, b = RedisMemoryStore.from_url(url, prefix=prefix), RedisMemoryStore.from_url(url, prefix=prefix)
        yield a, b
        await a.aclose(), await b.aclose()
    else:
        container = request.getfixturevalue("fake_cosmos")
        yield CosmosMemoryStore(container), CosmosMemoryStore(container)


async def test_missing_user_loads_an_empty_document(replicas):
    a, _ = replicas
    doc, _ = await a.load("nobody")
    assert doc.items == [] and doc.version == 0


async def test_save_then_load_round_trips(replicas):
    a, b = replicas
    doc, token = await a.load("u1")
    new = _doc("prefers short drafts", "signs as Ben")
    await a.save("u1", new, token, _expiry(new))
    loaded, _ = await b.load("u1")
    assert [i.text for i in loaded.items] == ["prefers short drafts", "signs as Ben"]
    assert loaded.version == 1


async def test_stale_writer_gets_a_conflict(replicas):
    a, b = replicas
    _, token_a = await a.load("u2")
    _, token_b = await b.load("u2")
    first = _doc("from replica a")
    await a.save("u2", first, token_a, _expiry(first))
    second = _doc("from replica b")
    with pytest.raises(MemoryConflict):
        await b.save("u2", second, token_b, _expiry(second))
    loaded, _ = await a.load("u2")
    assert [i.text for i in loaded.items] == ["from replica a"]


async def test_versions_increase_on_every_write(replicas):
    a, _ = replicas
    for n in range(3):
        doc, token = await a.load("u3")
        new = _doc(f"note {n}")
        await a.save("u3", new.model_copy(update={"version": doc.version}), token, _expiry(new))
    doc, _ = await a.load("u3")
    assert doc.version == 3


async def test_empty_document_is_deleted(replicas):
    a, _ = replicas
    doc, token = await a.load("u4")
    full = _doc("x")
    await a.save("u4", full, token, _expiry(full))
    doc, token = await a.load("u4")
    await a.save("u4", MemoryDoc(version=doc.version), token, None)
    again, _ = await a.load("u4")
    assert again.items == [] and again.version == 0


async def test_forget_deletes_unconditionally(replicas):
    a, b = replicas
    doc, token = await a.load("u5")
    full = _doc("x", "y")
    await a.save("u5", full, token, _expiry(full))
    await b.delete("u5")
    await b.delete("u5")  # idempotent
    loaded, _ = await a.load("u5")
    assert loaded.items == []


async def test_users_never_share_a_document(replicas):
    a, _ = replicas
    _, token = await a.load("alice")
    doc = _doc("alice's note")
    await a.save("alice", doc, token, _expiry(doc))
    bob, _ = await a.load("bob")
    assert bob.items == []


async def test_concurrent_writers_exactly_one_wins(replicas):
    a, b = replicas
    _, t1 = await a.load("race")
    _, t2 = await b.load("race")
    d1, d2 = _doc("one"), _doc("two")
    results = await asyncio.gather(a.save("race", d1, t1, _expiry(d1)), b.save("race", d2, t2, _expiry(d2)),
                                   return_exceptions=True)
    assert sum(isinstance(r, MemoryConflict) for r in results) == 1


async def test_redis_documents_expire_with_their_latest_item(redis_url):
    from redis.asyncio import Redis

    store = RedisMemoryStore.from_url(redis_url, prefix=f"ttl{time.time_ns()}:")
    doc = _doc("short lived", ttl=120)
    _, token = await store.load("u")
    await store.save("u", doc, token, _expiry(doc))
    raw = Redis.from_url(redis_url, decode_responses=True)
    ttl = await raw.ttl(f"{store._prefix}memory:u")
    assert 100 <= ttl <= 120
    await raw.aclose(), await store.aclose()


async def test_redis_acl_user_cannot_reach_other_prefixes(restricted_redis):
    """The VPS ACL is real: the agent's Redis user can't read another agent's keys."""
    from redis.asyncio import Redis
    from redis.exceptions import NoPermissionError

    url, prefix = restricted_redis
    client = Redis.from_url(url, decode_responses=True)
    with pytest.raises(NoPermissionError):
        await client.get("agentkit:someone-else:memory:x")
    await client.aclose()


async def test_cosmos_uses_prefixed_ids_and_item_ttl(fake_cosmos):
    container = fake_cosmos
    store = CosmosMemoryStore(container)
    doc = _doc("note", ttl=600)
    _, token = await store.load("u")
    await store.save("u", doc, token, _expiry(doc))
    stored = container.items["memory:u"]
    assert 590 <= stored["ttl"] <= 600 and stored["doc"]["version"] == 1


async def test_cosmos_expired_document_reads_as_empty_and_can_be_replaced(fake_cosmos):
    container = fake_cosmos
    store = CosmosMemoryStore(container)
    doc = _doc("note")
    _, token = await store.load("u")
    await store.save("u", doc, token, _expiry(doc))
    container.items["memory:u"]["expires_at"] = time.time() - 1  # TTL deletion can lag behind expiry
    empty, token = await store.load("u")
    assert empty.items == [] and token is not None
    fresh = _doc("fresh")
    await store.save("u", fresh, token, _expiry(fresh))
    loaded, _ = await store.load("u")
    assert [i.text for i in loaded.items] == ["fresh"]
