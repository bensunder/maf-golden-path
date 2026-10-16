"""Lessons, subjects and the journal on the real backends: Redis as the VPS stack's restricted per-agent user
(GET/SET/DEL/EVAL/PING on its own prefix only) and Cosmos (fake container with ETags and TTL). Also concurrent
writers, full journal pages, and old custom stores."""

import asyncio

import pytest

from agentkit.memory import (
    CosmosMemoryStore,
    InMemoryMemoryStore,
    LessonBook,
    MemoryService,
    MemoryUser,
    RedisMemoryStore,
    RunJournal,
    SubjectMemory,
)
from agentkit.memory.models import JournalEntry, MemoryDoc

SALT = "agent-stores-test-salt-0123456"


async def exercise(memory: MemoryService) -> None:
    lessons, subjects = LessonBook(memory, agent="payments"), SubjectMemory(memory, agent="payments")
    journal = RunJournal(memory, agent="payments", shards=2)
    lesson, _ = await lessons.propose("Code 51 means insufficient funds", origin="user", by="u_b")
    await lessons.approve(lesson.id, by="u_a")
    assert [i.status for i in await lessons.list()] == ["active"]
    await subjects.log("customer:1042", "recovery_draft_created", "draft_4da32bba")
    assert [e.note for e in await subjects.history("customer:1042")] == ["draft_4da32bba"]
    assert [s for s, _ in await subjects.recent()] == ["customer:1042"]
    now = memory.now()
    for i in range(5):
        await journal.record(JournalEntry(run_id=f"run-{i}", at=now - i, subjects=["customer:1042"]))
    entries, dropped = await journal.recent(hours=1)
    assert [e.run_id for e in entries] == [f"run-{i}" for i in range(5)] and dropped == 0
    await subjects.forget("customer:1042")
    assert await subjects.history("customer:1042") == []


async def test_redis_with_the_vps_agent_rights(restricted_redis):
    url, prefix = restricted_redis
    store = RedisMemoryStore.from_url(url, prefix=prefix)
    try:
        await exercise(MemoryService(store, salt=SALT, cache_seconds=0, writes_per_minute=0))
    finally:
        await store.aclose()


async def test_cosmos(fake_cosmos):
    await exercise(MemoryService(CosmosMemoryStore(fake_cosmos), salt=SALT, cache_seconds=0, writes_per_minute=0))
    assert all(item_id.startswith("memory:") for item_id in fake_cosmos.items)


async def test_concurrent_writers_lose_nothing():
    memory = MemoryService(InMemoryMemoryStore(), salt=SALT, cache_seconds=0, writes_per_minute=0)
    subjects, journal = SubjectMemory(memory, agent="payments"), RunJournal(memory, agent="payments", shards=1)
    now = memory.now()
    await asyncio.gather(*(subjects.log("customer:1", "contacted", f"attempt {i}") for i in range(3)),
                         *(journal.record(JournalEntry(run_id=f"r{i}", at=now)) for i in range(3)))
    assert sorted(e.note for e in await subjects.history("customer:1")) == [f"attempt {i}" for i in range(3)]
    assert len((await journal.recent(hours=1))[0]) == 3


async def test_a_full_journal_page_counts_what_it_drops():
    memory = MemoryService(InMemoryMemoryStore(), salt=SALT, cache_seconds=0, writes_per_minute=0)
    journal = RunJournal(memory, agent="payments", max_entries=10, shards=1)
    now = memory.now()
    for i in range(12):
        await journal.record(JournalEntry(run_id=f"r{i}", at=now))
    await journal.record(JournalEntry(run_id="r0", at=now))  # a retried write that already landed
    entries, dropped = await journal.recent(hours=1)
    assert len(entries) == 10 and dropped == 2


@pytest.mark.parametrize("field,value", [("retention_days", 0), ("max_entries", 5), ("shards", 17)])
def test_journal_bounds_are_validated(field, value):
    memory = MemoryService(InMemoryMemoryStore(), salt=SALT)
    with pytest.raises(ValueError):
        RunJournal(memory, agent="payments", **{field: value})


async def test_a_custom_store_written_for_0_10_still_serves_user_memory():
    class OldStore(InMemoryMemoryStore):
        async def load(self, user_key):  # the 0.10 signature: no model argument
            return await super().load(user_key, MemoryDoc)

    memory = MemoryService(OldStore(), salt=SALT, cache_seconds=0)
    await memory.remember(MemoryUser("ben@example.com"), "prefers short drafts")
    assert [i.text for i in await memory.recall(MemoryUser("ben@example.com"))] == ["prefers short drafts"]
