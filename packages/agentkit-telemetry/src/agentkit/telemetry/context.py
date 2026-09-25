"""Per-request context that every span in an agent run is tagged with."""

from __future__ import annotations

import contextvars
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = ["RunContext", "get_run_context", "run_context"]


@dataclass(frozen=True)
class RunContext:
    user_id: str | None = None
    session_id: str | None = None
    tenant_id: str | None = None
    request_id: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


_CURRENT: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar("agentkit_run_context", default=None)


def get_run_context() -> RunContext | None:
    return _CURRENT.get()


@contextmanager
def run_context(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    tenant_id: str | None = None,
    request_id: str | None = None,
    **attributes: Any,
) -> Iterator[RunContext]:
    """Set request context for everything inside the block (nested blocks merge)."""
    parent = _CURRENT.get() or RunContext()
    ctx = replace(
        parent,
        user_id=user_id or parent.user_id,
        session_id=session_id or parent.session_id,
        tenant_id=tenant_id or parent.tenant_id,
        request_id=request_id or parent.request_id,
        attributes={**parent.attributes, **attributes},
    )
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)
