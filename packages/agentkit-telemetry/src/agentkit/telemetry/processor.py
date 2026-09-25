"""Span processor that stamps request context and ownership onto every span."""

from __future__ import annotations

import hashlib
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from .context import get_run_context

__all__ = ["RunContextSpanProcessor"]


def _pseudonymize(value: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:32]


class RunContextSpanProcessor(SpanProcessor):
    """Adds user/session/tenant and static ownership attributes to spans at start.

    MAF's agent middleware runs *outside* the ``invoke_agent`` span, so setting
    attributes from middleware misses the chat and tool spans. A processor sees
    every span, including those created deep inside the function-invocation loop.
    """

    def __init__(
        self,
        *,
        static_attributes: dict[str, Any] | None = None,
        pseudonymize_user_ids: bool = True,
        salt: str = "agentkit",
    ) -> None:
        self._static = dict(static_attributes or {})
        self._pseudonymize = pseudonymize_user_ids
        self._salt = salt

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        for key, value in self._static.items():
            span.set_attribute(key, value)
        ctx = get_run_context()
        if ctx is None:
            return
        if ctx.user_id:
            span.set_attribute(
                "enduser.pseudo.id" if self._pseudonymize else "enduser.id",
                _pseudonymize(ctx.user_id, self._salt) if self._pseudonymize else ctx.user_id,
            )
        if ctx.session_id:
            span.set_attribute("session.id", ctx.session_id)
        if ctx.tenant_id:
            span.set_attribute("agentkit.tenant.id", ctx.tenant_id)
        if ctx.request_id:
            span.set_attribute("agentkit.request.id", ctx.request_id)
        for key, value in ctx.attributes.items():
            if isinstance(value, (str, bool, int, float)):
                span.set_attribute(f"agentkit.{key}", value)

    def on_end(self, span: ReadableSpan) -> None:  # pragma: no cover - nothing to do
        return None

    def shutdown(self) -> None:  # pragma: no cover
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # pragma: no cover
        return True
