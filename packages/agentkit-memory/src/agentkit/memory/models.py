"""The memory contract: stored documents and the HTTP API's request and response bodies.

Four kinds of document, all ``VersionedDoc`` so every store can compare-and-set them:

* ``MemoryDoc``: everything one agent remembers about one **user** (their stated preferences);
* ``LessonDoc``: the **agent's own notebook**, lessons and procedures shared by all its runs;
* ``SubjectDoc``: the history of one **subject** (a customer, an invoice), so autonomous runs remember cases;
* ``JournalPage``: one hour of the **run journal**, what each run did (never what was said).

Items are short, human-readable statements, so a person can read, edit or delete any of them
(docs/memory.md, principle 2).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EVENT_PATTERN",
    "ITEM_ID_PATTERN",
    "LESSON_ID_PATTERN",
    "SUBJECT_PATTERN",
    "JournalEntry",
    "JournalPage",
    "Lesson",
    "LessonDoc",
    "LessonOrigin",
    "LessonRevision",
    "LessonStatus",
    "RunTrigger",
    "SubjectDoc",
    "SubjectEvent",
    "SubjectIndexDoc",
    "VersionedDoc",
    "MemoryDoc",
    "MemoryItem",
    "MemoryItemIn",
    "MemoryItemOut",
    "MemoryItemPatch",
    "MemoryKind",
    "MemoryListOut",
    "MemorySource",
]

ITEM_ID_PATTERN = r"^mem_[0-9a-f]{12}$"
LESSON_ID_PATTERN = r"^les_[0-9a-f]{12}$"
EVENT_ID_PATTERN = r"^evt_[0-9a-f]{12}$"
#: ``<type>:<id>``, e.g. ``customer:1042`` or ``invoice:INV-2026-0042``. No spaces, no ``@``: ids, never contact details.
SUBJECT_PATTERN = r"^[a-z][a-z0-9_]{0,31}:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
#: What happened to a subject, as a short snake_case verb phrase (``recovery_draft_created``).
EVENT_PATTERN = r"^[a-z][a-z0-9_]{1,47}$"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_-]{8,128}$"

MemoryKind = Literal["preference", "fact"]
#: Who wrote the item: the agent (through its tool) or the person (through the API or console).
MemorySource = Literal["agent", "user"]


class VersionedDoc(BaseModel):
    """Any stored document. ``version`` increases on every write and drives optimistic concurrency."""

    model_config = ConfigDict(extra="forbid")

    version: int = 0
    updated_at: float = 0.0


class MemoryItem(BaseModel):
    """One remembered statement. Times are epoch seconds, like the session store's."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ITEM_ID_PATTERN)
    text: str
    kind: MemoryKind = "preference"
    source: MemorySource = "agent"
    created_at: float
    updated_at: float
    expires_at: float


class MemoryDoc(VersionedDoc):
    """Everything one agent remembers about one user, stored under one key.

    ``version`` increases on every write and drives optimistic concurrency, so two replicas writing
    for the same user never lose each other's changes. ``idempotency`` maps recent idempotency keys
    to the item they created, so a retried request returns the original item instead of a copy.
    """

    items: list[MemoryItem] = Field(default_factory=list)
    idempotency: dict[str, str] = Field(default_factory=dict)


# ------------------------------------------------------------------------------ agent lessons
#: ``pending`` waits for a memory admin; only ``active`` lessons reach the model; ``archived`` can be restored.
LessonStatus = Literal["pending", "active", "archived"]
#: Where a lesson came from. ``admin``: a memory admin wrote or stated it. ``user``: a signed-in person who isn't
#: an admin stated it. ``untrusted``: the run had read outside data (tool results, documents, e-mail) or had no
#: signed-in person (a scheduled run), so the lesson may have been planted. Only ``admin`` lessons skip review.
LessonOrigin = Literal["admin", "user", "untrusted"]
#: What started a run: a person, another agent on their behalf, a schedule or event, or nothing identifiable.
RunTrigger = Literal["user", "agent", "schedule", "system"]


class LessonRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["proposed", "created", "edited", "approved", "rejected", "archived", "restored", "reverted"]
    text: str
    at: float
    #: Pseudonymous id of whoever did it (an HMAC of their user id), or ``agent``.
    by: str


class Lesson(BaseModel):
    """One thing the agent has learned, e.g. "Stripe decline code 51 means insufficient funds"."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=LESSON_ID_PATTERN)
    text: str
    status: LessonStatus
    origin: LessonOrigin
    created_at: float
    updated_at: float
    expires_at: float
    #: The run that proposed it (journal ``run_id``), so a reviewer can see what the agent was doing.
    run_id: str | None = None
    #: Newest last, at most ``LessonPolicy.max_history``: who changed it, when, and to what. Rollback reads this.
    history: list[LessonRevision] = Field(default_factory=list)


class LessonDoc(VersionedDoc):
    items: list[Lesson] = Field(default_factory=list)


# ---------------------------------------------------------------------------- subject memory
class SubjectEvent(BaseModel):
    """Something that happened to a subject: ``recovery_draft_created``, note ``draft_4da32bba``."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=EVENT_ID_PATTERN)
    event: str = Field(pattern=EVENT_PATTERN)
    note: str = ""
    at: float
    expires_at: float
    run_id: str | None = None
    trigger: RunTrigger = "system"
    source: MemorySource = "agent"


class SubjectDoc(VersionedDoc):
    subject: str = ""
    events: list[SubjectEvent] = Field(default_factory=list)


class SubjectIndexDoc(VersionedDoc):
    """Which subjects have history, most recent last (subject -> last event time). Lets the console list them
    without scanning keys, which the VPS stack's Redis users aren't allowed to do."""

    subjects: dict[str, float] = Field(default_factory=dict)


# ------------------------------------------------------------------------------- run journal
class JournalEntry(BaseModel):
    """What one run did. Never prompt or reply text, never a raw user id."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    at: float
    duration_ms: int = 0
    trigger: RunTrigger = "system"
    #: Pseudonymous id of the person (HMAC), or "" for runs nobody started.
    actor: str = ""
    outcome: Literal["ok", "blocked", "error", "awaiting_approval"] = "ok"
    blocked_reason: str | None = None
    tools: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    lessons_proposed: int = 0


class JournalPage(VersionedDoc):
    """One UTC hour of the journal (``2026-10-15T23``). Hourly pages keep writes from contending on one key."""

    hour: str = ""
    entries: list[JournalEntry] = Field(default_factory=list)
    #: Entries not kept because the page was full (a run storm); the count still tells an operator.
    dropped: int = 0


# ---------------------------------------------------------------------------------------- API
class MemoryItemIn(BaseModel):
    """``POST /v1/memory``: a person adds something for the agent to remember."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)  # the policy enforces the real limit after cleaning
    kind: MemoryKind = "preference"


class MemoryItemPatch(BaseModel):
    """``PATCH /v1/memory/{id}``: a person corrects what the agent remembers."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)


class MemoryItemOut(BaseModel):
    id: str
    text: str
    kind: MemoryKind
    source: MemorySource
    created_at: float
    updated_at: float
    expires_at: float

    @classmethod
    def of(cls, item: MemoryItem) -> MemoryItemOut:
        return cls(**item.model_dump())


class MemoryListOut(BaseModel):
    items: list[MemoryItemOut]
    count: int
    max_items: int
    retention_days: int
