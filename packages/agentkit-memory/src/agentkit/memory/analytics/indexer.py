"""Keeps ``semantic_memory`` in step with the memory documents (lessons, subject events, user notes).

The Redis/Cosmos documents stay the source of truth; this is a searchable copy. ``MemoryService.notify`` calls
``SemanticIndexer.changed`` after every successful write or delete, and the indexer queues one replace of that
document's rows (never blocking the write; a full queue drops the update and the next write of that document
repairs it). What is indexed:

* **lessons**: only ``active`` ones (pending lessons haven't been reviewed and never reach the model, so they
  must not reach it through search either); agent-wide, per tenant.
* **subject events**: the event name and its note; agent-wide, per tenant, with the subject id in ``subject``.
* **user notes**: each person's remembered items, scoped to that person's pseudonymous key so a search on one
  person's behalf can never return another's.

All of this text has already passed the memory screening (no PII, no prompt injection) before it was stored.
"""

from __future__ import annotations

import logging
from typing import Any

from .store import AnalyticsStore, MemoryRecord

__all__ = ["SemanticIndexer", "reindex"]

logger = logging.getLogger(__name__)


class SemanticIndexer:
    def __init__(self, store: AnalyticsStore) -> None:
        self.store = store

    def changed(self, kind: str, *, key: str, doc: Any, tenant: str | None = None, agent: str | None = None,
                subject: str | None = None) -> None:
        try:
            if agent is not None and agent != self.store.agent:
                return  # another agent's memory in a shared process (tests, a gateway)
            prefix = f"{key}:"
            self.store.replace_memories(prefix, self._records(kind, key, doc, tenant, subject) if doc else [])
        except Exception as exc:  # the index must never fail a memory write
            logger.warning("semantic index not updated: %s", type(exc).__name__)

    def _records(self, kind: str, key: str, doc: Any, tenant: str | None, subject: str | None) -> list[MemoryRecord]:
        agent = self.store.agent
        if kind == "lessons":
            return [MemoryRecord(memory_id=f"{key}:{item.id}", agent=agent, kind="lesson", text=item.text,
                                 tenant=tenant, metadata={"lesson_id": item.id, "origin": item.origin},
                                 created_at=item.created_at, updated_at=item.updated_at, expires_at=item.expires_at)
                    for item in doc.items if item.status == "active"]
        if kind == "subject":
            name = subject or getattr(doc, "subject", "") or None
            return [MemoryRecord(memory_id=f"{key}:{event.id}", agent=agent, kind="subject_event",
                                 text=f"{event.event.replace('_', ' ')}: {event.note}" if event.note
                                 else event.event.replace("_", " "),
                                 tenant=tenant, subject=name,
                                 metadata={"event": event.event, "note": event.note, "run_id": event.run_id},
                                 created_at=event.at, updated_at=event.at, expires_at=event.expires_at)
                    for event in doc.events]
        if kind == "user":
            return [MemoryRecord(memory_id=f"{key}:{item.id}", agent=agent, kind="user_note", text=item.text,
                                 tenant=tenant, scope=key, metadata={"item_id": item.id, "kind": item.kind},
                                 created_at=item.created_at, updated_at=item.updated_at, expires_at=item.expires_at)
                    for item in doc.items]
        return []


async def reindex(am: Any, tenant: str | None) -> None:
    """Rebuild semantic memory for one tenant from the memory store: lessons and every subject in the index.
    (People's own notes are re-indexed the next time each is written; their keys can't be listed.)"""
    from ..models import LessonDoc, SubjectDoc

    memory = am.memory
    try:
        key = am.lessons.key(tenant)
        memory.notify("lessons", key=key, doc=await memory.load_doc("lessons.list", key, LessonDoc), tenant=tenant,
                      agent=am.lessons.agent)
        for subject, _ in await am.subjects.recent(tenant_id=tenant, limit=10_000):
            skey = am.subjects.key(subject, tenant)
            doc = await memory.load_doc("subject.history", skey, SubjectDoc)
            memory.notify("subject", key=skey, doc=doc if doc.events else None, tenant=tenant,
                          agent=am.subjects.agent, subject=subject)
    except Exception as exc:
        logger.warning("reindex stopped: %s", type(exc).__name__)
