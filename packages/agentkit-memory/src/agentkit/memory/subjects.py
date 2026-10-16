"""Subject memory: what happened to one customer, invoice or case, across every run (docs/memory.md).

User memory is keyed to the person talking, so a scheduled run (nobody talking) has none. Subject memory is
keyed to *what the work is about*, so any run, by anyone or by a schedule, sees the same history:

    customer:1042   recovery_draft_created   "draft_4da32bba"
    customer:1042   payment_promised         "for 2026-10-20"

Rules:

* subjects are ids (``<type>:<id>``), never names or contact details; notes are screened like every memory
  (no personal data, no instructions) and short. The record of truth (names, amounts, e-mails) stays in the
  business system; memory keeps ids and outcomes;
* events are append-only for the agent, and each expires ``retention_days`` after it was written;
* a subject keeps at most ``max_events`` (the oldest go first); an index of recent subjects lets the console
  list them without scanning keys (the VPS Redis users can't).
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass

from .errors import MemoryError_, MemoryRejected
from .models import EVENT_PATTERN, SUBJECT_PATTERN, MemorySource, RunTrigger, SubjectDoc, SubjectEvent, SubjectIndexDoc
from .policy import MemoryPolicy
from .service import MemoryService

__all__ = ["SubjectMemory", "SubjectPolicy", "check_subject"]

logger = logging.getLogger(__name__)

_DAY = 86_400.0
_SUBJECT = re.compile(SUBJECT_PATTERN)
_EVENT = re.compile(EVENT_PATTERN)
_DIGIT_RUN = re.compile(r"\d{13,19}")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _looks_personal(ident: str) -> bool:
    """Card or social security numbers in an id. (The pattern already rules out e-mail addresses and spaces;
    ordinary numeric ids, even long ones, are fine: a broad PII check would mistake them for phone numbers.)"""
    return bool(_SSN.search(ident)) or any(_luhn(m.group()) for m in _DIGIT_RUN.finditer(ident))


def check_subject(subject: str) -> str:
    """The subject id, normalised (type lower-cased), or ``MemoryRejected``."""
    value = (subject or "").strip()
    if ":" in value:
        kind, _, ident = value.partition(":")
        value = f"{kind.lower()}:{ident}"
    if not _SUBJECT.fullmatch(value):
        raise MemoryRejected("A subject is an id like customer:1042 or invoice:INV-2026-0042.", code="bad_subject")
    if _looks_personal(value):
        raise MemoryRejected("A subject is an id, not a card or social security number.", code="personal_data")
    return value


@dataclass(frozen=True)
class SubjectPolicy:
    max_events: int = 200
    retention_days: int = 365
    max_index: int = 1000

    def __post_init__(self) -> None:
        for name, low, high in (("max_events", 1, 2000), ("retention_days", 1, 3650), ("max_index", 10, 10000)):
            if not low <= getattr(self, name) <= high:
                raise ValueError(f"{name} must be between {low} and {high}")

    def live(self, doc: SubjectDoc, now: float) -> SubjectDoc:
        events = [e for e in doc.events if e.expires_at > now]
        return doc if len(events) == len(doc.events) else doc.model_copy(update={"events": events})

    def append(self, doc: SubjectDoc, subject: str, event: SubjectEvent, now: float) -> SubjectDoc:
        events = [*self.live(doc, now).events, event][-self.max_events:]
        return doc.model_copy(update={"subject": subject, "events": events, "updated_at": now})

    @staticmethod
    def expiry(doc: SubjectDoc) -> float | None:
        return max((e.expires_at for e in doc.events), default=None)

    def touch(self, index: SubjectIndexDoc, subject: str, now: float) -> SubjectIndexDoc:
        cutoff = now - self.retention_days * _DAY
        subjects = {s: t for s, t in index.subjects.items() if t > cutoff and s != subject}
        subjects[subject] = now
        if len(subjects) > self.max_index:
            subjects = dict(sorted(subjects.items(), key=lambda kv: kv[1])[-self.max_index:])
        return index.model_copy(update={"subjects": subjects, "updated_at": now})

    def drop(self, index: SubjectIndexDoc, subject: str, now: float) -> SubjectIndexDoc:
        if subject not in index.subjects:
            return index
        return index.model_copy(update={"subjects": {s: t for s, t in index.subjects.items() if s != subject},
                                        "updated_at": now})

    def index_expiry(self, index: SubjectIndexDoc) -> float | None:
        return (max(index.subjects.values()) + self.retention_days * _DAY) if index.subjects else None


class SubjectMemory:
    def __init__(self, memory: MemoryService, *, agent: str, policy: SubjectPolicy | None = None,
                 screen: MemoryPolicy | None = None) -> None:
        self.memory = memory
        self.agent = agent
        self.policy = policy or SubjectPolicy()
        self.screen = screen or memory.policy

    def key(self, subject: str, tenant_id: str | None) -> str:
        return "subject:" + self.memory.digest("subject", self.agent, tenant_id, subject)

    def index_key(self, tenant_id: str | None) -> str:
        return "subjects:" + self.memory.digest("subjects", self.agent, tenant_id)

    async def history(self, subject: str, *, tenant_id: str | None = None) -> list[SubjectEvent]:
        """The subject's events, oldest first."""
        subject = check_subject(subject)
        doc = await self.memory.load_doc("subject.history", self.key(subject, tenant_id), SubjectDoc)
        return list(self.policy.live(doc, self.memory.now()).events)

    async def log(self, subject: str, event: str, note: str = "", *, run_id: str | None = None,
                  trigger: RunTrigger = "system", source: MemorySource = "agent",
                  tenant_id: str | None = None) -> SubjectEvent:
        subject = check_subject(subject)
        if not _EVENT.fullmatch(event or ""):
            self.memory.count("subject.log", "bad_event")
            raise MemoryRejected("An event is a short snake_case phrase like recovery_draft_created.",
                                 code="bad_event")
        clean = ""
        if note and note.strip():
            try:
                clean = await self.screen.clean(note)
            except MemoryRejected as exc:
                self.memory.count("subject.log", exc.code)
                raise
        key = self.key(subject, tenant_id)
        self.memory.rate_limit("subject.log", key)
        now = self.memory.now()
        item = SubjectEvent(id=f"evt_{secrets.token_hex(6)}", event=event, note=clean, at=now,
                            expires_at=now + self.policy.retention_days * _DAY, run_id=run_id, trigger=trigger,
                            source=source)
        await self.memory.write_doc("subject.log", key, SubjectDoc,
                                    lambda doc, t: self.policy.append(doc, subject, item, t), self.policy.expiry)
        try:  # the event is stored; the index only helps people find subjects, so it never fails the write
            await self.memory.write_doc("subject.index", self.index_key(tenant_id), SubjectIndexDoc,
                                        lambda doc, t: self.policy.touch(doc, subject, t), self.policy.index_expiry)
        except MemoryError_ as exc:
            logger.warning("subject index not updated: %s", exc.code)
        return item

    async def recent(self, *, tenant_id: str | None = None, limit: int = 100) -> list[tuple[str, float]]:
        """Subjects with history, most recent first: ``[(subject, last_event_time)]``."""
        doc = await self.memory.load_doc("subject.index", self.index_key(tenant_id), SubjectIndexDoc)
        cutoff = self.memory.now() - self.policy.retention_days * _DAY
        items = sorted(((s, t) for s, t in doc.subjects.items() if t > cutoff), key=lambda kv: kv[1], reverse=True)
        return items[: max(1, limit)]

    async def forget(self, subject: str, *, tenant_id: str | None = None) -> None:
        """Erase a subject's history (an erasure request, or a mistake). Idempotent."""
        subject = check_subject(subject)
        await self.memory.delete_doc("subject.forget", self.key(subject, tenant_id))
        await self.memory.write_doc("subject.index", self.index_key(tenant_id), SubjectIndexDoc,
                                    lambda doc, t: self.policy.drop(doc, subject, t), self.policy.index_expiry)
