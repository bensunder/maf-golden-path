"""Memory rules. Pure functions over ``MemoryDoc``: no I/O, no clock of their own, no mutation of inputs.

What may be remembered (docs/memory.md):
* short statements a person could read in a second (``max_chars``);
* never personal data: e-mail addresses, phone numbers, card and social security numbers are refused,
  not redacted, because a redacted note ("call me at [REDACTED_PHONE]") is useless and misleading;
* never instructions to the agent: text that reads like a prompt injection is refused, so a poisoned
  memory can't become a standing order in every later conversation;
* at most ``max_items`` per user; when full, the least recently updated item makes room;
* every item expires ``ttl_days`` after its last update.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field

from agentkit.guardrails import HeuristicInjectionDetector, redact_pii

from .errors import MemoryNotFound, MemoryRejected
from .models import MemoryDoc, MemoryItem, MemoryKind, MemorySource

__all__ = ["MemoryPolicy", "new_item_id"]

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_IDEMPOTENCY_KEEP = 20


def new_item_id() -> str:
    return f"mem_{secrets.token_hex(6)}"


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


@dataclass(frozen=True)
class MemoryPolicy:
    max_items: int = 50
    max_chars: int = 280
    ttl_days: int = 180
    detector: HeuristicInjectionDetector = field(default_factory=HeuristicInjectionDetector)

    def __post_init__(self) -> None:
        if not 1 <= self.max_items <= 500:
            raise ValueError("max_items must be between 1 and 500")
        if not 20 <= self.max_chars <= 1000:
            raise ValueError("max_chars must be between 20 and 1000")
        if not 1 <= self.ttl_days <= 3650:
            raise ValueError("ttl_days must be between 1 and 3650")

    @property
    def ttl_seconds(self) -> float:
        return self.ttl_days * 86_400.0

    # ------------------------------------------------------------------------------- screening
    async def clean(self, raw: str) -> str:
        """The text as it would be stored, or ``MemoryRejected`` with the reason."""
        text = _SPACE.sub(" ", _CONTROL.sub(" ", raw or "")).strip()
        if not text:
            raise MemoryRejected("Nothing to remember.", code="empty")
        if len(text) > self.max_chars:
            raise MemoryRejected(f"Keep it under {self.max_chars} characters.", code="too_long")
        if redact_pii(text) != text:
            raise MemoryRejected("Memory can't hold contact details, card or ID numbers.", code="personal_data")
        verdict = await self.detector.analyze(text)
        if verdict.attack:
            raise MemoryRejected("Memory can't hold instructions to the agent.", code="instructions")
        return text

    # ---------------------------------------------------------------------------- doc changes
    def live(self, doc: MemoryDoc, now: float) -> MemoryDoc:
        """The document without expired items (and without idempotency keys that point at them)."""
        items = [i for i in doc.items if i.expires_at > now]
        if len(items) == len(doc.items):
            return doc
        ids = {i.id for i in items}
        return doc.model_copy(update={
            "items": items,
            "idempotency": {k: v for k, v in doc.idempotency.items() if v in ids},
        })

    def add(
        self,
        doc: MemoryDoc,
        text: str,
        *,
        kind: MemoryKind,
        source: MemorySource,
        now: float,
        idempotency_key: str | None = None,
    ) -> tuple[MemoryDoc, MemoryItem, bool]:
        """Add ``text`` (already cleaned). Returns ``(doc, item, created)``.

        * a known idempotency key returns its original item unchanged (``created`` False);
        * the same text already remembered is refreshed, not duplicated (``created`` False);
        * when full, the least recently updated item is dropped to make room.
        """
        doc = self.live(doc, now)
        if idempotency_key and (existing_id := doc.idempotency.get(idempotency_key)):
            existing = next((i for i in doc.items if i.id == existing_id), None)
            if existing is not None:
                return doc, existing, False

        expires = now + self.ttl_seconds
        items = list(doc.items)
        duplicate = next((i for i in items if _same(i.text, text)), None)
        if duplicate is not None:
            item = duplicate.model_copy(update={"updated_at": now, "expires_at": expires, "kind": kind})
            items = [item if i.id == duplicate.id else i for i in items]
            created = False
        else:
            taken = {i.id for i in items}
            item_id = new_item_id()
            while item_id in taken:
                item_id = new_item_id()
            item = MemoryItem(id=item_id, text=text, kind=kind, source=source,
                              created_at=now, updated_at=now, expires_at=expires)
            items.append(item)
            if len(items) > self.max_items:
                items.sort(key=lambda i: i.updated_at)
                items = items[len(items) - self.max_items:]
            created = True

        idempotency = dict(doc.idempotency)
        if idempotency_key:
            idempotency[idempotency_key] = item.id
            while len(idempotency) > _IDEMPOTENCY_KEEP:
                idempotency.pop(next(iter(idempotency)))
        ids = {i.id for i in items}
        idempotency = {k: v for k, v in idempotency.items() if v in ids}
        return doc.model_copy(update={"items": items, "idempotency": idempotency, "updated_at": now}), item, created

    def edit(self, doc: MemoryDoc, item_id: str, text: str, *, now: float) -> tuple[MemoryDoc, MemoryItem]:
        """Replace an item's text (already cleaned). A person's edit makes the item theirs (``source`` user)."""
        doc = self.live(doc, now)
        current = next((i for i in doc.items if i.id == item_id), None)
        if current is None:
            raise MemoryNotFound("No such memory.")
        clash = next((i for i in doc.items if i.id != item_id and _same(i.text, text)), None)
        if clash is not None:
            raise MemoryRejected("That is already remembered.", code="duplicate")
        item = current.model_copy(update={"text": text, "source": "user", "updated_at": now,
                                          "expires_at": now + self.ttl_seconds})
        items = [item if i.id == item_id else i for i in doc.items]
        return doc.model_copy(update={"items": items, "updated_at": now}), item

    def remove(self, doc: MemoryDoc, item_id: str, *, now: float) -> tuple[MemoryDoc, bool]:
        """Drop one item. Returns ``(doc, removed)``; removing what isn't there is not an error."""
        doc = self.live(doc, now)
        items = [i for i in doc.items if i.id != item_id]
        if len(items) == len(doc.items):
            return doc, False
        idempotency = {k: v for k, v in doc.idempotency.items() if v != item_id}
        return doc.model_copy(update={"items": items, "idempotency": idempotency, "updated_at": now}), True

    @staticmethod
    def expiry(doc: MemoryDoc) -> float | None:
        """When the whole document can be deleted: its latest item's expiry, or None when empty."""
        return max((i.expires_at for i in doc.items), default=None)
