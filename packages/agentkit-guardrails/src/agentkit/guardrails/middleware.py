"""Guardrail middleware for MAF agents.

Layer map (MAF has three middleware layers):

* agent layer   — :class:`InputGuardMiddleware`, :class:`SessionTokenBudgetMiddleware`
* chat layer    — :class:`PiiRedactionMiddleware` (what the model sees)
* function layer — :class:`ToolPolicyMiddleware`, :class:`ToolOutputShieldMiddleware`
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from agent_framework import AgentMiddleware, AgentResponse, ChatMiddleware, FunctionMiddleware

from .detectors import InjectionDetector
from .refusal import refuse

__all__ = [
    "InputGuardMiddleware",
    "PiiRedactionMiddleware",
    "SessionTokenBudgetMiddleware",
    "ToolOutputShieldMiddleware",
    "ToolPolicyMiddleware",
]

logger = logging.getLogger(__name__)

DEFAULT_REFUSAL = "I can't help with that request."


class InputGuardMiddleware(AgentMiddleware):
    """Screens new user input for prompt injection before any model call."""

    def __init__(
        self,
        detector: InjectionDetector,
        *,
        refusal_message: str = DEFAULT_REFUSAL,
        max_input_chars: int | None = 20_000,
    ) -> None:
        self._detector = detector
        self._refusal = refusal_message
        self._max_chars = max_input_chars

    async def process(self, context, call_next) -> None:  # type: ignore[override]
        user_text = "\n".join(m.text for m in context.messages if m.role == "user" and m.text)
        if self._max_chars and len(user_text) > self._max_chars:
            logger.info("input rejected: %d chars > %d", len(user_text), self._max_chars)
            refuse(context, "That message is too long. Please shorten it and try again.", "input_too_long")
            return
        verdict = await self._detector.analyze(user_text)
        if verdict.attack:
            logger.warning("prompt injection blocked by %s (%s)", verdict.detector, verdict.detail)
            refuse(context, self._refusal, f"prompt_injection:{verdict.detector}")
            return
        await call_next()


class ToolOutputShieldMiddleware(FunctionMiddleware):
    """Scans tool results for indirect prompt injection before the model reads them."""

    WITHHELD = "[tool output withheld: it contained instructions that look like a prompt-injection attempt]"

    def __init__(self, detector: InjectionDetector, *, min_chars: int = 40) -> None:
        self._detector = detector
        self._min_chars = min_chars

    @staticmethod
    def _as_text(result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        if isinstance(result, Iterable) and not isinstance(result, (bytes, Mapping)):
            parts = [getattr(r, "text", None) or str(getattr(r, "result", "") or r) for r in result]
            return "\n".join(p for p in parts if p)
        return str(result)

    async def process(self, context, call_next) -> None:  # type: ignore[override]
        await call_next()
        text = self._as_text(context.result)
        if len(text) < self._min_chars:
            return
        verdict = await self._detector.analyze("", [text])
        if verdict.attack:
            logger.warning("tool %s output withheld (%s)", context.function.name, verdict.detail)
            context.result = self.WITHHELD


ArgumentValidator = Callable[[Mapping[str, Any]], "str | None"]


class ToolPolicyMiddleware(FunctionMiddleware):
    """Allow/deny lists and argument validators for tool calls.

    A denied call is never executed; the model receives an explanation instead, so it can
    recover (ask the user, pick another tool) rather than the run crashing.
    For human approval of sensitive tools, use MAF's native ``@tool(approval_mode="always_require")``.
    """

    def __init__(
        self,
        *,
        allowed: Iterable[str] | None = None,
        denied: Iterable[str] = (),
        validators: Mapping[str, ArgumentValidator] | None = None,
    ) -> None:
        self._allowed = set(allowed) if allowed is not None else None
        self._denied = set(denied)
        self._validators = dict(validators or {})

    def _decide(self, name: str, arguments: Mapping[str, Any]) -> str | None:
        if name in self._denied or (self._allowed is not None and name not in self._allowed):
            return f"Tool '{name}' is not permitted by policy."
        validator = self._validators.get(name)
        if validator:
            problem = validator(arguments)
            if problem:
                return f"Tool '{name}' call rejected: {problem}"
        return None

    async def process(self, context, call_next) -> None:  # type: ignore[override]
        args = context.arguments
        args = args.model_dump() if hasattr(args, "model_dump") else dict(args or {})
        problem = self._decide(context.function.name, args)
        if problem:
            logger.warning(problem)
            context.result = problem
            return
        await call_next()


_PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "CARD": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "PHONE": re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)"),
}


def _luhn_ok(candidate: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", candidate)]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def redact_pii(text: str, kinds: Iterable[str] = ("EMAIL", "SSN", "CARD", "PHONE")) -> str:
    for kind in kinds:
        pattern = _PII_PATTERNS[kind]
        if kind == "CARD":
            text = pattern.sub(lambda m: "[REDACTED_CARD]" if _luhn_ok(m.group()) else m.group(), text)
        else:
            text = pattern.sub(f"[REDACTED_{kind}]", text)
    return text


class PiiRedactionMiddleware(ChatMiddleware):
    """Redacts PII in user messages before they reach the model (and session history).

    Regex-based: good for obvious identifiers. For regulated data use Azure AI Language
    PII detection or Purview (``agent-framework-purview``) behind the same seam.
    """

    def __init__(self, kinds: Iterable[str] = ("EMAIL", "SSN", "CARD", "PHONE")) -> None:
        unknown = set(kinds) - set(_PII_PATTERNS)
        if unknown:
            raise ValueError(f"unknown PII kinds: {sorted(unknown)}")
        self._kinds = tuple(kinds)

    async def process(self, context, call_next) -> None:  # type: ignore[override]
        for message in context.messages:
            if message.role != "user":
                continue
            for content in message.contents:
                if content.type == "text" and content.text:
                    content.text = redact_pii(content.text, self._kinds)
        await call_next()


class SessionTokenBudgetMiddleware(AgentMiddleware):
    """Caps total model tokens per session (state persists with the session).

    Per-run caps on tool loops come from MAF's ``FunctionInvocationConfiguration``; team-level
    quotas belong on the gateway (APIM ``llm-token-limit``). This covers the gap between them:
    one runaway conversation.
    """

    STATE_KEY = "agentkit.tokens_used"

    def __init__(self, max_tokens: int, *, message: str | None = None) -> None:
        self._max = max_tokens
        self._message = message or "This conversation has reached its usage limit. Please start a new one."

    def _add(self, context, response: AgentResponse) -> AgentResponse:
        usage = response.usage_details or {}
        total = usage.get("total_token_count") or (
            (usage.get("input_token_count") or 0) + (usage.get("output_token_count") or 0)
        )
        if context.session is not None and total:
            context.session.state[self.STATE_KEY] = context.session.state.get(self.STATE_KEY, 0) + total
        return response

    async def process(self, context, call_next) -> None:  # type: ignore[override]
        session = context.session
        if session is not None and session.state.get(self.STATE_KEY, 0) >= self._max:
            refuse(context, self._message, "session_token_budget")
            return
        if context.stream:
            context.stream_result_hooks.append(lambda r: self._add(context, r))
        await call_next()
        if not context.stream and isinstance(context.result, AgentResponse):
            self._add(context, context.result)
