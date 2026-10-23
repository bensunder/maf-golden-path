"""``EpisodeMiddleware``: one ``agent_episodes`` row per agent run, written in the background.

It sits right after the run-metrics middleware (like the journal), so it also records runs a guardrail refuses.
Per run it records who/what started it (pseudonymous), timing, outcome, the model and token counts, and every
tool call with its duration and success, timed by a function middleware the ``AnalyticsProvider`` adds to the
run. With ``AGENTKIT_ANALYTICS_CAPTURE=redacted`` it also keeps the run's text **after PII redaction**: the
latest input, the conversation messages sent to the model, the output, and each tool call's arguments. Raw text
is never stored, and capture is off by default.

A full write queue drops the row and counts it; the analytics store can never slow down or fail a reply.
"""

from __future__ import annotations

import contextvars
import json
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_framework import (
    AgentMiddleware,
    AgentResponse,
    FunctionMiddleware,
    ResponseStream,
)
from agentkit.guardrails import redact_pii
from agentkit.telemetry import get_run_context
from agentkit.telemetry.middleware import BLOCKED_KEY

from ..journal import run_trigger
from .store import AnalyticsStore, Episode, ToolCallRecord

__all__ = ["EpisodeMiddleware", "EpisodeRecorder", "ToolTimingMiddleware", "current_recorder", "redact"]

_MAX_TEXT = 4000
_MAX_MESSAGES = 20
_MAX_TOOL_CALLS = 50
_MAX_ARGS = 500


def redact(text: str | None, limit: int = _MAX_TEXT) -> str | None:
    """PII-redacted (e-mail, SSN, card, phone) and truncated text, or None."""
    if not text:
        return None
    out = redact_pii(str(text))
    return out if len(out) <= limit else out[: limit - 1] + "…"


@dataclass
class EpisodeRecorder:
    capture: bool
    started: float = field(default_factory=time.perf_counter)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

    def tool(self, name: str, ok: bool, duration_ms: int, arguments: Any, result: Any) -> None:
        if len(self.tool_calls) >= _MAX_TOOL_CALLS:
            return
        args = None
        if self.capture and arguments is not None:
            try:
                args = redact(json.dumps(_plain(arguments), default=str), _MAX_ARGS)
            except (TypeError, ValueError):
                args = None
        self.tool_calls.append(ToolCallRecord(name=name, ok=ok, duration_ms=duration_ms, arguments_redacted=args,
                                              result_chars=len(str(result)) if result is not None else 0))


_CURRENT: contextvars.ContextVar[EpisodeRecorder | None] = contextvars.ContextVar("agentkit_episode", default=None)
_BY_REQUEST: OrderedDict[str, EpisodeRecorder] = OrderedDict()
_BY_REQUEST_MAX = 10_000


def current_recorder() -> EpisodeRecorder | None:
    ctx = get_run_context()
    if ctx and ctx.request_id and (rec := _BY_REQUEST.get(ctx.request_id)):
        return rec
    return _CURRENT.get()


def _plain(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    return dump() if callable(dump) else value


class ToolTimingMiddleware(FunctionMiddleware):
    """Times each tool call of one run into its recorder (added per run by ``AnalyticsProvider``)."""

    def __init__(self, recorder: EpisodeRecorder) -> None:
        self.recorder = recorder

    async def process(self, context: Any, call_next: Any) -> None:  # type: ignore[override]
        name = getattr(getattr(context, "function", None), "name", None) or "unknown"
        started, ok = time.perf_counter(), False
        try:
            await call_next()
            ok = True
        finally:
            self.recorder.tool(name, ok, int((time.perf_counter() - started) * 1000),
                               getattr(context, "arguments", None), getattr(context, "result", None))


def _text(message: Any) -> str:
    text = getattr(message, "text", None)
    return text if isinstance(text, str) else ""


def _role(message: Any) -> str:
    role = getattr(message, "role", "")
    return str(getattr(role, "value", role) or "")


def _usage(response: AgentResponse | None) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage_details", None)
    if usage is None:
        return None, None
    get = usage.get if isinstance(usage, dict) else (lambda k: getattr(usage, k, None))
    return get("input_token_count"), get("output_token_count")


def _model(context: Any, response: AgentResponse | None) -> str | None:
    raw = getattr(response, "raw_representation", None)
    for candidate in (getattr(raw, "model_id", None), getattr(getattr(context.agent, "client", None), "model_id", None),
                      getattr(getattr(context.agent, "chat_client", None), "model_id", None)):
        if isinstance(candidate, str) and candidate:
            return candidate[:120]
    return None


def _outcome(response: AgentResponse | None) -> tuple[str, str | None]:
    if response is None:
        return "error", None
    blocked = (response.additional_properties or {}).get(BLOCKED_KEY)
    if blocked:
        return "blocked", str(blocked).split(":", 1)[0][:40]
    if getattr(response, "user_input_requests", None):
        return "awaiting_approval", None
    return "ok", None


class EpisodeMiddleware(AgentMiddleware):
    def __init__(self, store: AnalyticsStore, *, capture: str = "off",
                 pseudonym: Callable[[str | None, str | None], str] | None = None) -> None:
        if capture not in ("off", "redacted"):
            raise ValueError("capture is 'off' or 'redacted'")
        self.store = store
        self.capture = capture
        self._pseudonym = pseudonym or (lambda user, tenant: "")

    async def process(self, context: Any, call_next: Any) -> None:  # type: ignore[override]
        ctx = get_run_context()
        request_id = ctx.request_id if ctx else None
        recorder = EpisodeRecorder(capture=self.capture == "redacted")
        if request_id:
            _BY_REQUEST[request_id] = recorder
            _BY_REQUEST.move_to_end(request_id)
            while len(_BY_REQUEST) > _BY_REQUEST_MAX:
                _BY_REQUEST.popitem(last=False)
        token = _CURRENT.set(recorder)
        started_at = time.time()
        messages = list(context.messages or [])
        done = False

        def write(response: AgentResponse | None) -> None:
            nonlocal done
            if done:
                return
            done = True
            if request_id:
                _BY_REQUEST.pop(request_id, None)
            outcome, reason = _outcome(response)
            tokens_in, tokens_out = _usage(response)
            episode = Episode(
                run_id=request_id or f"run_{uuid.uuid4().hex[:16]}", agent=self.store.agent, started_at=started_at,
                duration_ms=int((time.perf_counter() - recorder.started) * 1000), trigger=run_trigger(),
                actor=self._pseudonym(ctx.user_id, ctx.tenant_id) if ctx else "",
                tenant=ctx.tenant_id if ctx else None, session=getattr(context.session, "session_id", None),
                outcome=outcome, blocked_reason=reason, model=_model(context, response), input_tokens=tokens_in,
                output_tokens=tokens_out, capture=self.capture, tool_calls=list(recorder.tool_calls))
            if recorder.capture:
                users = [m for m in messages if _role(m) == "user"]
                episode.input_redacted = redact(_text(users[-1]) if users else None)
                episode.prompt_redacted = [{"role": _role(m), "text": redact(_text(m)) or ""}
                                           for m in messages[-_MAX_MESSAGES:]]
                episode.output_redacted = redact(getattr(response, "text", None) if response else None)
            self.store.record_episode(episode)

        try:
            if context.stream:
                async def _on_final(final: AgentResponse) -> AgentResponse:
                    write(final)
                    return final

                context.stream_result_hooks.append(_on_final)
            try:
                await call_next()
            except Exception:
                write(None)
                raise
            if not isinstance(context.result, ResponseStream):
                write(context.result)
        finally:
            _CURRENT.reset(token)
