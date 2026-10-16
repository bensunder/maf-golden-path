"""The run journal: a dated record of what every run of an agent did (docs/memory.md, "Run journal").

Written automatically after each run (interactive, agent-to-agent or scheduled) by ``JournalMiddleware``:
when, what started it, the outcome, which tools ran, which subjects it touched, how many lessons it proposed.
It never holds prompt or reply text, and people appear only as pseudonyms (an HMAC of their id).

The agent can read its recent journal ("what did I do for customer:1042 yesterday?") and the console shows it.
Entries live in hourly pages, split into a few shards so concurrent runs rarely contend on one key; each page
expires ``retention_days`` after its hour.

``RunLedger`` is the per-run scratchpad the middleware, the memory provider and the memory tools share: the
tools a run called, the subjects it touched, and whether it read outside data (which makes any lesson it
proposes ``untrusted``). It is found by the run's ``request_id`` (set for every turn by the conversation
service, and still current while a streamed reply is being produced) or, for direct ``agent.run`` calls, by
a context variable.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from agent_framework import AgentMiddleware, AgentResponse, ResponseStream

from agentkit.telemetry import get_run_context
from agentkit.telemetry.middleware import BLOCKED_KEY

from .errors import MemoryError_
from .models import JournalEntry, JournalPage, RunTrigger
from .service import MemoryService

__all__ = ["JournalMiddleware", "RunJournal", "RunLedger", "current_ledger", "run_trigger"]

logger = logging.getLogger(__name__)
_HOUR = 3600.0
_DAY = 86_400.0
_MAX_TOOLS = 30
_MAX_SUBJECTS = 20
_READ_CONCURRENCY = 16


@dataclass
class RunLedger:
    run_id: str
    trigger: RunTrigger
    actor: str
    started: float = field(default_factory=time.perf_counter)
    tools: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    #: True once the run has seen a tool result other than memory's own: outside data reached the model.
    tainted: bool = False
    lessons_proposed: int = 0

    def tool(self, name: str | None) -> None:
        if name and name not in self.tools and len(self.tools) < _MAX_TOOLS:
            self.tools.append(name)

    def subject(self, subject: str) -> None:
        if subject not in self.subjects and len(self.subjects) < _MAX_SUBJECTS:
            self.subjects.append(subject)


_CURRENT: contextvars.ContextVar[RunLedger | None] = contextvars.ContextVar("agentkit_memory_ledger", default=None)
_BY_REQUEST: OrderedDict[str, RunLedger] = OrderedDict()
_BY_REQUEST_MAX = 10_000


def _register(request_id: str | None, ledger: RunLedger) -> None:
    if not request_id:
        return
    _BY_REQUEST[request_id] = ledger
    _BY_REQUEST.move_to_end(request_id)
    while len(_BY_REQUEST) > _BY_REQUEST_MAX:  # abandoned streams never finish; never let them pile up
        _BY_REQUEST.popitem(last=False)


def _unregister(request_id: str | None) -> None:
    if request_id:
        _BY_REQUEST.pop(request_id, None)


def current_ledger() -> RunLedger | None:
    """The ledger of the run in progress, if a ``JournalMiddleware`` started one."""
    ctx = get_run_context()
    if ctx and ctx.request_id and (ledger := _BY_REQUEST.get(ctx.request_id)):
        return ledger
    return _CURRENT.get()


def run_trigger() -> RunTrigger:
    """What started the current run: a person, an agent on someone's behalf, a schedule, or nothing known.

    A scheduler or event handler marks its runs with ``run_context(trigger="schedule")``."""
    ctx = get_run_context()
    if ctx is None:
        return "system"
    if ctx.attributes.get("caller_chain"):
        return "agent"
    if ctx.user_id:
        return "user"
    return "schedule" if ctx.attributes.get("trigger") == "schedule" else "system"


class RunJournal:
    def __init__(self, memory: MemoryService, *, agent: str, retention_days: int = 90, max_entries: int = 500,
                 shards: int = 4) -> None:
        if not 1 <= retention_days <= 3650:
            raise ValueError("journal retention_days must be between 1 and 3650")
        if not 10 <= max_entries <= 5000:
            raise ValueError("journal max_entries must be between 10 and 5000")
        if not 1 <= shards <= 16:
            raise ValueError("journal shards must be between 1 and 16")
        self.memory = memory
        self.agent = agent
        self.retention_days = retention_days
        self.max_entries = max_entries
        self.shards = shards
        self._agent_digest = memory.digest("journal", agent)

    @staticmethod
    def hour_of(at: float) -> str:
        return time.strftime("%Y-%m-%dT%H", time.gmtime(at))

    def key(self, hour: str, shard: int) -> str:
        return f"journal:{self._agent_digest}:{hour}:{shard}"

    def _shard(self, run_id: str) -> int:
        return zlib.crc32(run_id.encode()) % self.shards

    async def record(self, entry: JournalEntry) -> None:
        hour = self.hour_of(entry.at)
        hour_end = (int(entry.at // _HOUR) + 1) * _HOUR
        expires = hour_end + self.retention_days * _DAY

        def change(page: JournalPage, now: float) -> JournalPage:
            if any(e.run_id == entry.run_id for e in page.entries):
                return page  # a retried write that already landed
            if len(page.entries) >= self.max_entries:
                return page.model_copy(update={"hour": hour, "dropped": page.dropped + 1, "updated_at": now})
            return page.model_copy(update={"hour": hour, "entries": [*page.entries, entry], "updated_at": now})

        await self.memory.write_doc("journal.record", self.key(hour, self._shard(entry.run_id)), JournalPage,
                                    change, lambda _page: expires)

    async def recent(self, *, hours: int = 24, subject: str | None = None, limit: int = 200,
                     until: float | None = None) -> tuple[list[JournalEntry], int]:
        """Entries from the last ``hours`` (newest first) and how many were dropped from full pages."""
        hours = max(1, min(int(hours), 24 * 7))
        end = until if until is not None else self.memory.now()
        keys = [self.key(self.hour_of(end - h * _HOUR), s) for h in range(hours) for s in range(self.shards)]
        gate = asyncio.Semaphore(_READ_CONCURRENCY)  # a week is 672 page reads: don't open them all at once

        async def read(key: str) -> JournalPage:
            async with gate:
                return await self.memory.load_doc("journal.read", key, JournalPage)

        pages = await asyncio.gather(*(read(k) for k in keys))
        cutoff = end - hours * _HOUR
        entries = [e for p in pages for e in p.entries if cutoff < e.at <= end
                   and (subject is None or subject in e.subjects)]
        entries.sort(key=lambda e: e.at, reverse=True)
        return entries[: max(1, limit)], sum(p.dropped for p in pages)


def _tools_in(response: AgentResponse | None) -> list[str]:
    names: list[str] = []
    for message in getattr(response, "messages", None) or []:
        for content in getattr(message, "contents", None) or []:
            if getattr(content, "type", None) == "function_call" and getattr(content, "name", None):
                names.append(content.name)
    return names


def _outcome(response: AgentResponse | None) -> tuple[str, str | None]:
    if response is None:
        return "error", None
    blocked = (response.additional_properties or {}).get(BLOCKED_KEY)
    if blocked:
        return "blocked", str(blocked).split(":", 1)[0][:40]
    if getattr(response, "user_input_requests", None):
        return "awaiting_approval", None
    return "ok", None


class JournalMiddleware(AgentMiddleware):
    """Records every run in the journal. Put it right after the run-metrics middleware, so it also sees runs a
    guardrail refuses. A journal outage never fails a run: the entry is dropped and counted."""

    def __init__(self, journal: RunJournal) -> None:
        self.journal = journal

    async def process(self, context: Any, call_next: Any) -> None:  # type: ignore[override]
        ctx = get_run_context()
        request_id = ctx.request_id if ctx else None
        memory = self.journal.memory
        ledger = RunLedger(run_id=request_id or f"run_{uuid.uuid4().hex[:16]}", trigger=run_trigger(),
                           actor=memory.pseudonym(ctx.user_id, ctx.tenant_id) if ctx else "")
        _register(request_id, ledger)
        token = _CURRENT.set(ledger)
        at = memory.now()

        async def write(response: AgentResponse | None) -> None:
            outcome, reason = _outcome(response)
            for name in _tools_in(response):
                ledger.tool(name)
            entry = JournalEntry(run_id=ledger.run_id, at=at, trigger=ledger.trigger, actor=ledger.actor,
                                 duration_ms=int((time.perf_counter() - ledger.started) * 1000), outcome=outcome,
                                 blocked_reason=reason, tools=ledger.tools, subjects=ledger.subjects,
                                 lessons_proposed=ledger.lessons_proposed)
            try:
                await self.journal.record(entry)
            except MemoryError_ as exc:
                logger.warning("journal entry dropped: %s", exc.code)
            except Exception as exc:  # never let the journal break a reply
                memory.count("journal.record", "error")
                logger.warning("journal entry dropped: %s", type(exc).__name__)
            finally:
                _unregister(request_id)

        try:
            if context.stream:
                async def _on_final(final: AgentResponse) -> AgentResponse:
                    await write(final)
                    return final

                context.stream_result_hooks.append(_on_final)
            try:
                await call_next()
            except Exception:
                await write(None)
                raise
            if not isinstance(context.result, ResponseStream):
                await write(context.result)
        finally:
            _CURRENT.reset(token)
