"""Shrink API responses before the model reads them.

Enterprise APIs return far more than the model needs. Every byte costs tokens, dilutes
attention, and may carry data the agent shouldn't see. A shaper picks fields, caps list
length and enforces a character budget, and it tells the model when it truncated.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["Shaper"]

_MISSING = object()


def _get_path(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


def _set_path(target: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


@dataclass(frozen=True)
class Shaper:
    """``fields``: dot paths to keep from each object (``["id", "status", "customer.name"]``).
    ``items_key``: where the list lives in a wrapper object (``"value"`` for OData, ``"items"``, …).
    ``drop``: dot paths to always remove (e.g. ``["customer.ssn"]``), applied when ``fields`` is empty.
    """

    fields: Sequence[str] = ()
    drop: Sequence[str] = ()
    max_items: int = 20
    max_chars: int = 4000
    items_key: str | None = None

    def _pick(self, obj: Any) -> Any:
        if not isinstance(obj, dict):
            return obj
        if self.fields:
            out: dict = {}
            for path in self.fields:
                value = _get_path(obj, path)
                if value is not _MISSING:
                    _set_path(out, path, value)
            return out
        if self.drop:
            obj = json.loads(json.dumps(obj))
            for path in self.drop:
                parts = path.split(".")
                parent = _get_path(obj, ".".join(parts[:-1])) if len(parts) > 1 else obj
                if isinstance(parent, dict):
                    parent.pop(parts[-1], None)
        return obj

    def shape(self, data: Any) -> str:
        if isinstance(data, str):
            text, note = data, None
        else:
            items = data
            wrapper_total = None
            if self.items_key and isinstance(data, dict) and isinstance(data.get(self.items_key), list):
                items = data[self.items_key]
                wrapper_total = data.get("count") or data.get("total") or data.get("@odata.count")
            note = None
            if isinstance(items, list):
                total = wrapper_total or len(items)
                kept = [self._pick(i) for i in items[: self.max_items]]
                if len(items) > self.max_items or (wrapper_total and wrapper_total > len(kept)):
                    note = f"showing {len(kept)} of {total} results; ask for a narrower query to see others"
                shaped: Any = kept
            else:
                shaped = self._pick(items)
            text = json.dumps(shaped, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(text) > self.max_chars:
            text = text[: self.max_chars]
            note = (note + "; " if note else "") + f"output truncated to {self.max_chars} characters"
        return text + (f"\n[{note}]" if note else "")


DEFAULT_SHAPER = Shaper()
