"""Agent lessons: the agent's own notebook, shared by every run of one agent (docs/memory.md, "Agent memory").

A lesson is a short procedure or fact about the work, not about a person: "Stripe decline code 51 means
insufficient funds; a retry after the 1st usually succeeds". Because every later run reads them, lessons
are the one kind of memory where a planted note would spread, so they are governed more tightly:

* screened like user memory (no personal data, nothing that reads as an instruction to the agent);
* **reviewed**: a lesson goes live at once only when a memory admin wrote or stated it in a run that had
  not read outside data. Everything else waits as ``pending`` until an admin approves it, and pending
  lessons never reach the model;
* **attributed and versioned**: each lesson keeps who changed it, when and to what (pseudonymously), and an
  admin can roll back to an earlier text or restore an archived lesson;
* bounded (active, pending and archived counts) and expiring.

``LessonPolicy`` is pure (no I/O, no clock); ``LessonBook`` applies it through the shared ``MemoryService``
(timeouts, circuit breaker, compare-and-set, rate limit).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from .errors import MemoryNotFound, MemoryRejected
from .models import Lesson, LessonDoc, LessonOrigin, LessonRevision, LessonStatus
from .policy import MemoryPolicy
from .service import MemoryService

__all__ = ["LessonBook", "LessonPolicy", "new_lesson_id"]

_DAY = 86_400.0


def new_lesson_id() -> str:
    return f"les_{secrets.token_hex(6)}"


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


@dataclass(frozen=True)
class LessonPolicy:
    max_active: int = 100
    max_pending: int = 50
    max_archived: int = 50
    max_history: int = 10
    #: Active lessons are kept this long after their last change; pending and archived ones for ``review_days``.
    retention_days: int = 365
    review_days: int = 30

    def __post_init__(self) -> None:
        for name, low, high in (("max_active", 1, 500), ("max_pending", 1, 500), ("max_archived", 0, 500),
                                ("max_history", 1, 50), ("retention_days", 1, 3650), ("review_days", 1, 365)):
            if not low <= getattr(self, name) <= high:
                raise ValueError(f"{name} must be between {low} and {high}")

    def _expiry(self, status: LessonStatus, now: float) -> float:
        return now + (self.retention_days if status == "active" else self.review_days) * _DAY

    def _revise(self, lesson: Lesson, action: str, text: str, by: str, now: float, **changes) -> Lesson:
        history = [*lesson.history, LessonRevision(action=action, text=text, at=now, by=by)][-self.max_history:]
        status = changes.get("status", lesson.status)
        return lesson.model_copy(update={"text": text, "updated_at": now, "expires_at": self._expiry(status, now),
                                         "history": history, **changes})

    # ---------------------------------------------------------------------------------- reads
    @staticmethod
    def live(doc: LessonDoc, now: float) -> LessonDoc:
        items = [i for i in doc.items if i.expires_at > now]
        return doc if len(items) == len(doc.items) else doc.model_copy(update={"items": items})

    @staticmethod
    def find(doc: LessonDoc, lesson_id: str) -> Lesson:
        lesson = next((i for i in doc.items if i.id == lesson_id), None)
        if lesson is None:
            raise MemoryNotFound("No such lesson.")
        return lesson

    # -------------------------------------------------------------------------------- changes
    def propose(self, doc: LessonDoc, text: str, *, origin: LessonOrigin, by: str, run_id: str | None,
                now: float) -> tuple[LessonDoc, Lesson, bool]:
        """Add a lesson (text already screened). ``admin`` lessons go live; the rest wait for review.

        Returns ``(doc, lesson, created)``. Text already known (in any status but archived) is not duplicated:
        an active lesson is refreshed, a pending one stays pending, and a known lesson is never demoted."""
        doc = self.live(doc, now)
        status: LessonStatus = "active" if origin == "admin" else "pending"
        existing = next((i for i in doc.items if _same(i.text, text) and i.status != "archived"), None)
        if existing is not None:
            if existing.status == "pending" and status == "active":
                lesson = self._revise(existing, "approved", existing.text, by, now, status="active", origin="admin")
            else:
                lesson = existing.model_copy(update={"updated_at": now,
                                                     "expires_at": self._expiry(existing.status, now)})
            return self._replace(doc, lesson, now), lesson, False
        if status == "pending" and sum(1 for i in doc.items if i.status == "pending") >= self.max_pending:
            raise MemoryRejected("Too many lessons are waiting for review; ask an admin to review them first.",
                                 code="review_queue_full")
        taken = {i.id for i in doc.items}
        lesson_id = new_lesson_id()
        while lesson_id in taken:
            lesson_id = new_lesson_id()
        lesson = Lesson(id=lesson_id, text=text, status=status, origin=origin, created_at=now, updated_at=now,
                        expires_at=self._expiry(status, now), run_id=run_id,
                        history=[LessonRevision(action="created" if status == "active" else "proposed",
                                                text=text, at=now, by=by)])
        return self._bounded(doc.model_copy(update={"items": [*doc.items, lesson], "updated_at": now})), lesson, True

    def approve(self, doc: LessonDoc, lesson_id: str, *, by: str, now: float) -> tuple[LessonDoc, Lesson]:
        doc = self.live(doc, now)
        lesson = self.find(doc, lesson_id)
        if lesson.status != "pending":
            raise MemoryRejected("Only a lesson waiting for review can be approved.", code="not_pending")
        lesson = self._revise(lesson, "approved", lesson.text, by, now, status="active")
        return self._bounded(self._replace(doc, lesson, now)), lesson

    def reject(self, doc: LessonDoc, lesson_id: str, *, by: str, now: float) -> tuple[LessonDoc, Lesson]:
        """Turn down a pending lesson. It is archived (restorable for ``review_days``), not deleted."""
        doc = self.live(doc, now)
        lesson = self.find(doc, lesson_id)
        if lesson.status != "pending":
            raise MemoryRejected("Only a lesson waiting for review can be rejected.", code="not_pending")
        lesson = self._revise(lesson, "rejected", lesson.text, by, now, status="archived")
        return self._bounded(self._replace(doc, lesson, now)), lesson

    def edit(self, doc: LessonDoc, lesson_id: str, text: str, *, by: str, now: float) -> tuple[LessonDoc, Lesson]:
        """An admin corrects a lesson (text already screened). An edited pending lesson stays pending."""
        doc = self.live(doc, now)
        lesson = self.find(doc, lesson_id)
        if lesson.status == "archived":
            raise MemoryRejected("Restore the lesson before editing it.", code="archived")
        if any(i.id != lesson_id and i.status != "archived" and _same(i.text, text) for i in doc.items):
            raise MemoryRejected("That lesson already exists.", code="duplicate")
        lesson = self._revise(lesson, "edited", text, by, now)
        return self._replace(doc, lesson, now), lesson

    def archive(self, doc: LessonDoc, lesson_id: str, *, by: str, now: float) -> tuple[LessonDoc, bool]:
        """Take a lesson out of use (restorable for ``review_days``). Idempotent."""
        doc = self.live(doc, now)
        lesson = next((i for i in doc.items if i.id == lesson_id), None)
        if lesson is None or lesson.status == "archived":
            return doc, False
        lesson = self._revise(lesson, "archived", lesson.text, by, now, status="archived")
        return self._bounded(self._replace(doc, lesson, now)), True

    def restore(self, doc: LessonDoc, lesson_id: str, *, by: str, now: float) -> tuple[LessonDoc, Lesson]:
        """Bring an archived lesson back as active (an admin is restoring it, which is a review)."""
        doc = self.live(doc, now)
        lesson = self.find(doc, lesson_id)
        if lesson.status != "archived":
            raise MemoryRejected("Only an archived lesson can be restored.", code="not_archived")
        if any(i.id != lesson_id and i.status != "archived" and _same(i.text, lesson.text) for i in doc.items):
            raise MemoryRejected("That lesson already exists.", code="duplicate")
        lesson = self._revise(lesson, "restored", lesson.text, by, now, status="active")
        return self._bounded(self._replace(doc, lesson, now)), lesson

    def revert(self, doc: LessonDoc, lesson_id: str, revision: int, *, by: str,
               now: float) -> tuple[LessonDoc, Lesson]:
        """Put back the text of an earlier revision (index into ``history``, oldest first)."""
        doc = self.live(doc, now)
        lesson = self.find(doc, lesson_id)
        if lesson.status == "archived":
            raise MemoryRejected("Restore the lesson before rolling it back.", code="archived")
        if not 0 <= revision < len(lesson.history):
            raise MemoryNotFound("No such revision.")
        text = lesson.history[revision].text
        if any(i.id != lesson_id and i.status != "archived" and _same(i.text, text) for i in doc.items):
            raise MemoryRejected("That lesson already exists.", code="duplicate")
        lesson = self._revise(lesson, "reverted", text, by, now)
        return self._replace(doc, lesson, now), lesson

    # -------------------------------------------------------------------------------- helpers
    @staticmethod
    def _replace(doc: LessonDoc, lesson: Lesson, now: float) -> LessonDoc:
        return doc.model_copy(update={"items": [lesson if i.id == lesson.id else i for i in doc.items],
                                      "updated_at": now})

    def _bounded(self, doc: LessonDoc) -> LessonDoc:
        """Keep each status within its limit, dropping the least recently updated."""
        keep: list[Lesson] = []
        for status, limit in (("active", self.max_active), ("pending", self.max_pending),
                              ("archived", self.max_archived)):
            group = sorted((i for i in doc.items if i.status == status), key=lambda i: i.updated_at)
            keep.extend(group[max(0, len(group) - limit):])
        if len(keep) == len(doc.items):
            return doc
        ids = {i.id for i in keep}
        return doc.model_copy(update={"items": [i for i in doc.items if i.id in ids]})

    @staticmethod
    def expiry(doc: LessonDoc) -> float | None:
        return max((i.expires_at for i in doc.items), default=None)


class LessonBook:
    """One agent's lessons, per tenant. Every write goes through the screening of ``MemoryPolicy.clean``."""

    def __init__(self, memory: MemoryService, *, agent: str, policy: LessonPolicy | None = None,
                 screen: MemoryPolicy | None = None) -> None:
        self.memory = memory
        self.agent = agent
        self.policy = policy or LessonPolicy()
        self.screen = screen or memory.policy

    def key(self, tenant_id: str | None) -> str:
        return "lessons:" + self.memory.digest("lessons", self.agent, tenant_id)

    async def list(self, tenant_id: str | None = None) -> list[Lesson]:
        """Every lesson (any status), most recently updated first."""
        doc = await self.memory.load_doc("lessons.list", self.key(tenant_id), LessonDoc)
        items = self.policy.live(doc, self.memory.now()).items
        return sorted(items, key=lambda i: i.updated_at, reverse=True)

    async def active(self, tenant_id: str | None = None) -> list[Lesson]:
        return [i for i in await self.list(tenant_id) if i.status == "active"]

    async def _change(self, op: str, tenant_id: str | None, change) -> LessonDoc:
        key = self.key(tenant_id)
        doc = await self.memory.write_doc(op, key, LessonDoc, change, self.policy.expiry)
        self.memory.notify("lessons", key=key, doc=doc, tenant=tenant_id, agent=self.agent)
        return doc

    async def propose(self, text: str, *, origin: LessonOrigin, by: str, run_id: str | None = None,
                      tenant_id: str | None = None) -> tuple[Lesson, bool]:
        clean = await self._screen("lessons.propose", text)
        self.memory.rate_limit("lessons.propose", self.key(tenant_id))
        out: dict = {}

        def change(doc: LessonDoc, now: float) -> LessonDoc:
            new, out["lesson"], out["created"] = self.policy.propose(doc, clean, origin=origin, by=by,
                                                                     run_id=run_id, now=now)
            return new

        await self._change("lessons.propose", tenant_id, change)
        return out["lesson"], out["created"]

    async def approve(self, lesson_id: str, *, by: str, tenant_id: str | None = None) -> Lesson:
        return await self._one("lessons.approve", tenant_id, lambda d, now: self.policy.approve(d, lesson_id, by=by, now=now))

    async def reject(self, lesson_id: str, *, by: str, tenant_id: str | None = None) -> Lesson:
        return await self._one("lessons.reject", tenant_id, lambda d, now: self.policy.reject(d, lesson_id, by=by, now=now))

    async def restore(self, lesson_id: str, *, by: str, tenant_id: str | None = None) -> Lesson:
        return await self._one("lessons.restore", tenant_id, lambda d, now: self.policy.restore(d, lesson_id, by=by, now=now))

    async def revert(self, lesson_id: str, revision: int, *, by: str, tenant_id: str | None = None) -> Lesson:
        return await self._one("lessons.revert", tenant_id,
                               lambda d, now: self.policy.revert(d, lesson_id, revision, by=by, now=now))

    async def edit(self, lesson_id: str, text: str, *, by: str, tenant_id: str | None = None) -> Lesson:
        clean = await self._screen("lessons.edit", text)
        return await self._one("lessons.edit", tenant_id,
                               lambda d, now: self.policy.edit(d, lesson_id, clean, by=by, now=now))

    async def archive(self, lesson_id: str, *, by: str, tenant_id: str | None = None) -> bool:
        out = {"removed": False}

        def change(doc: LessonDoc, now: float) -> LessonDoc:
            new, out["removed"] = self.policy.archive(doc, lesson_id, by=by, now=now)
            return new

        await self._change("lessons.archive", tenant_id, change)
        return out["removed"]

    async def _one(self, op: str, tenant_id: str | None, apply) -> Lesson:
        # Reviews by memory admins aren't rate limited (approving a backlog is legitimate); proposals are.
        out: dict = {}

        def change(doc: LessonDoc, now: float) -> LessonDoc:
            new, out["lesson"] = apply(doc, now)
            return new

        await self._change(op, tenant_id, change)
        return out["lesson"]

    async def _screen(self, op: str, text: str) -> str:
        try:
            return await self.screen.clean(text)
        except MemoryRejected as exc:
            self.memory.count(op, exc.code)
            raise
