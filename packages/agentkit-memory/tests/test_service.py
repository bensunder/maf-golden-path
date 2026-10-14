"""Use cases and resilience: keys, isolation, retries on conflict, breaker, timeouts, cache, rate limit."""

import asyncio

import pytest

from agentkit.memory import (
    CircuitBreaker,
    InMemoryMemoryStore,
    MemoryConflict,
    MemoryNotFound,
    MemoryPolicy,
    MemoryRateLimited,
    MemoryRejected,
    MemoryService,
    MemoryUnavailable,
    MemoryUser,
)

SALT = "unit-test-salt-0123456789"
BEN = MemoryUser("ben@example.com")
ANA = MemoryUser("ana@example.com")


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FlakyStore(InMemoryMemoryStore):
    """In-memory store that can fail, hang, or lose a write race on demand."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = False
        self.hang = False
        self.conflicts = 0
        self.loads = 0

    async def load(self, user_key):
        self.loads += 1
        if self.hang:
            await asyncio.sleep(10)
        if self.fail:
            raise ConnectionError("store down")
        return await super().load(user_key)

    async def save(self, user_key, doc, token, expires_at):
        if self.fail:
            raise ConnectionError("store down")
        if self.conflicts:
            self.conflicts -= 1
            raise MemoryConflict("raced")
        return await super().save(user_key, doc, token, expires_at)


def service(store=None, **kw):
    kw.setdefault("cache_seconds", 0)
    return MemoryService(store or InMemoryMemoryStore(), salt=SALT, **kw)


# ------------------------------------------------------------------------------------- keys
def test_keys_are_hmac_of_tenant_and_user_never_the_raw_id():
    svc = service()
    key = svc.key_for(BEN)
    assert len(key) == 32 and "ben" not in key and key == svc.key_for(MemoryUser("ben@example.com"))
    assert key != svc.key_for(MemoryUser("ben@example.com", tenant_id="contoso"))
    assert key != MemoryService(InMemoryMemoryStore(), salt="another-salt-0123456789").key_for(BEN)


def test_short_salt_is_refused():
    with pytest.raises(ValueError, match="16"):
        MemoryService(InMemoryMemoryStore(), salt="short")


# ---------------------------------------------------------------------------------- use cases
async def test_remember_recall_edit_delete_round_trip():
    svc = service()
    item, created = await svc.remember(BEN, "prefers short drafts")
    assert created
    assert [i.text for i in await svc.recall(BEN)] == ["prefers short drafts"]
    edited = await svc.edit(BEN, item.id, "prefers very short drafts")
    assert edited.source == "user" and (await svc.recall(BEN))[0].text == "prefers very short drafts"
    assert await svc.delete(BEN, item.id) is True
    assert await svc.delete(BEN, item.id) is False
    assert await svc.recall(BEN) == []


async def test_recall_is_newest_first():
    clock = Clock()
    svc = service(clock=clock)
    await svc.remember(BEN, "first note")
    clock.t += 5
    await svc.remember(BEN, "second note")
    assert [i.text for i in await svc.recall(BEN)] == ["second note", "first note"]


async def test_users_are_isolated():
    svc = service()
    await svc.remember(BEN, "ben likes tea")
    await svc.remember(ANA, "ana likes coffee")
    assert [i.text for i in await svc.recall(BEN)] == ["ben likes tea"]
    assert [i.text for i in await svc.recall(ANA)] == ["ana likes coffee"]
    item = (await svc.recall(ANA))[0]
    with pytest.raises(MemoryNotFound):
        await svc.edit(BEN, item.id, "hijack")  # Ben can't touch Ana's item even with its id
    assert await svc.delete(BEN, item.id) is False and len(await svc.recall(ANA)) == 1


async def test_forget_all_only_affects_that_user():
    svc = service()
    await svc.remember(BEN, "one note")
    await svc.remember(ANA, "two note")
    await svc.forget_all(BEN)
    assert await svc.recall(BEN) == [] and len(await svc.recall(ANA)) == 1


async def test_rejections_store_nothing():
    svc = service()
    with pytest.raises(MemoryRejected):
        await svc.remember(BEN, "my card 4242 4242 4242 4242")
    assert await svc.recall(BEN) == []


async def test_idempotent_remember_returns_the_same_item():
    svc = service()
    first, created = await svc.remember(BEN, "note a", source="user", idempotency_key="req-12345678")
    again, created_again = await svc.remember(BEN, "note a", source="user", idempotency_key="req-12345678")
    assert created and not created_again and again.id == first.id


async def test_export_is_readable_markdown():
    svc = service()
    await svc.remember(BEN, "prefers short drafts")
    text = await svc.export_markdown(BEN, agent_name="Payments")
    assert text.startswith("# What Payments remembers about you") and "- prefers short drafts" in text
    assert "Nothing is remembered" in await svc.export_markdown(ANA, agent_name="Payments")


async def test_memory_expires_after_retention():
    clock = Clock()
    svc = service(clock=clock, policy=MemoryPolicy(ttl_days=1))
    await svc.remember(BEN, "short lived")
    clock.t += 2 * 86_400
    assert await svc.recall(BEN) == []


# -------------------------------------------------------------------------------- resilience
async def test_conflicts_are_retried():
    store = FlakyStore()
    store.conflicts = 2
    svc = service(store)
    item, created = await svc.remember(BEN, "survives two races")
    assert created and [i.text for i in await svc.recall(BEN)] == ["survives two races"]


async def test_persistent_conflict_is_reported():
    store = FlakyStore()
    store.conflicts = 99
    with pytest.raises(MemoryConflict):
        await service(store).remember(BEN, "never lands")


class YieldingStore(InMemoryMemoryStore):
    """Yields between load and save, so concurrent writers really interleave and race."""

    async def load(self, user_key):
        result = await super().load(user_key)
        await asyncio.sleep(0)
        return result


async def test_racing_writers_never_lose_a_successful_write():
    svc = service(YieldingStore())
    results = await asyncio.gather(*(svc.remember(BEN, f"note number {n}") for n in range(6)),
                                   return_exceptions=True)
    landed = {r[0].text for r in results if not isinstance(r, BaseException)}
    failed = [r for r in results if isinstance(r, BaseException)]
    assert all(isinstance(r, MemoryConflict) for r in failed)  # a loser is told, never silently dropped
    assert landed and landed == {i.text for i in await svc.recall(BEN)}


async def test_store_failure_is_unavailable_and_opens_the_breaker():
    store = FlakyStore()
    store.fail = True
    clock = Clock(0.0)
    svc = service(store, breaker=CircuitBreaker(threshold=2, cooldown=30, clock=clock))
    for _ in range(2):
        with pytest.raises(MemoryUnavailable):
            await svc.recall(BEN)
    loads = store.loads
    with pytest.raises(MemoryUnavailable) as exc:
        await svc.recall(BEN)
    assert store.loads == loads, "an open breaker must not touch the store"
    assert exc.value.retry_after >= 1
    store.fail = False
    clock.t = 31.0  # half-open: one trial call goes through and closes the breaker
    assert await svc.recall(BEN) == []
    assert await svc.recall(BEN) == []


async def test_store_timeout_is_unavailable():
    store = FlakyStore()
    store.hang = True
    svc = service(store, timeout_seconds=0.05)
    with pytest.raises(MemoryUnavailable):
        await svc.recall(BEN)


async def test_reads_are_cached_and_writes_invalidate():
    store = FlakyStore()
    svc = MemoryService(store, salt=SALT, cache_seconds=60)
    await svc.recall(BEN)
    await svc.recall(BEN)
    assert store.loads == 1
    await svc.remember(BEN, "fresh note")
    assert [i.text for i in await svc.recall(BEN)] == ["fresh note"]


async def test_writes_are_rate_limited_per_user():
    mono = Clock(0.0)
    svc = MemoryService(InMemoryMemoryStore(), salt=SALT, writes_per_minute=2, monotonic=mono, cache_seconds=0)
    await svc.remember(BEN, "one one")
    await svc.remember(BEN, "two two")
    with pytest.raises(MemoryRateLimited) as exc:
        await svc.remember(BEN, "three three")
    assert 1 <= exc.value.retry_after <= 61
    await svc.remember(ANA, "ana is not limited by ben")
    mono.t = 61.0
    await svc.remember(BEN, "after the window")
