"""The memory contract: stored documents and the HTTP API's request and response bodies.

One document holds everything an agent remembers about one user. Items are short, human-readable
statements, so a person can read, edit or delete any of them (docs/memory.md, principle 2).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ITEM_ID_PATTERN",
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
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_-]{8,128}$"

MemoryKind = Literal["preference", "fact"]
#: Who wrote the item: the agent (through its tool) or the person (through the API or console).
MemorySource = Literal["agent", "user"]


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


class MemoryDoc(BaseModel):
    """Everything one agent remembers about one user, stored under one key.

    ``version`` increases on every write and drives optimistic concurrency, so two replicas writing
    for the same user never lose each other's changes. ``idempotency`` maps recent idempotency keys
    to the item they created, so a retried request returns the original item instead of a copy.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = 0
    items: list[MemoryItem] = Field(default_factory=list)
    idempotency: dict[str, str] = Field(default_factory=dict)
    updated_at: float = 0.0


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
