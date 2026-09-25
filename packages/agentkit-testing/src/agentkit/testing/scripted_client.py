"""A deterministic chat client for unit-testing MAF agents without a model.

``ScriptedChatClient`` is composed from the same layers as MAF's real clients
(function invocation, chat middleware, telemetry), so tools, middleware and
spans behave exactly as they do in production. Only the model call is scripted.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Union

from agent_framework import (
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    Message,
    UsageDetails,
)
from agent_framework.observability import ChatTelemetryLayer

__all__ = [
    "ScriptExhaustedError",
    "ScriptedChatClient",
    "ToolCall",
    "Turn",
    "reply",
    "tool_call",
]


class ScriptExhaustedError(AssertionError):
    """The agent asked the model for more turns than the test scripted."""


@dataclass(frozen=True)
class ToolCall:
    """A tool call the scripted model will emit."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass(frozen=True)
class Turn:
    """One scripted model turn: text, tool calls, or both, plus optional usage."""

    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: UsageDetails | None = None


def reply(text: str, *, input_tokens: int | None = None, output_tokens: int | None = None) -> Turn:
    """A model turn that answers with plain text."""
    return Turn(text=text, usage=_usage(input_tokens, output_tokens))


def tool_call(name: str, /, **arguments: Any) -> Turn:
    """A model turn that calls a single tool with keyword arguments."""
    return Turn(tool_calls=(ToolCall(name=name, arguments=arguments),))


ScriptItem = Union[str, Turn, ToolCall, Message, Callable[[Sequence[Message]], Union[str, Turn, Message]]]


def _usage(input_tokens: int | None, output_tokens: int | None) -> UsageDetails | None:
    if input_tokens is None and output_tokens is None:
        return None
    total = (input_tokens or 0) + (output_tokens or 0)
    return UsageDetails(
        input_token_count=input_tokens, output_token_count=output_tokens, total_token_count=total
    )


@dataclass
class RecordedCall:
    """What the agent sent to the model on one call."""

    messages: list[Message]
    options: dict[str, Any]

    @property
    def instructions(self) -> str:
        """System instructions the agent sent (MAF passes them as an option, not a message)."""
        value = self.options.get("instructions") or ""
        return value if isinstance(value, str) else "\n".join(value)

    @property
    def last_user_text(self) -> str:
        for message in reversed(self.messages):
            if message.role == "user":
                return message.text
        return ""

    def function_results(self) -> list[Content]:
        return [c for m in self.messages for c in m.contents if c.type == "function_result"]


class _RawScriptedClient(BaseChatClient):
    OTEL_PROVIDER_NAME = "agentkit.scripted"

    def __init__(self, *, script: Sequence[ScriptItem] = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._script: list[ScriptItem] = list(script)
        self.calls: list[RecordedCall] = []
        self._call_counter = 0

    # -- script management -------------------------------------------------
    def enqueue(self, *items: ScriptItem) -> None:
        self._script.extend(items)

    @property
    def remaining(self) -> int:
        return len(self._script)

    def assert_script_consumed(self) -> None:
        if self._script:
            raise AssertionError(f"{len(self._script)} scripted turn(s) were never used: {self._script!r}")

    # -- assertions ----------------------------------------------------------
    def tool_calls_made(self) -> list[ToolCall]:
        """Every tool call the scripted model emitted, in order."""
        found: list[ToolCall] = []
        for call in self.calls:
            for message in call.messages:
                for content in message.contents:
                    if content.type == "function_call":
                        args = content.arguments
                        if isinstance(args, str):
                            args = json.loads(args or "{}")
                        found.append(ToolCall(name=content.name or "", arguments=dict(args or {}), call_id=content.call_id))
        # Calls appear once per later model call that carries history; de-duplicate by call id.
        seen: set[str | None] = set()
        unique = []
        for c in found:
            if c.call_id not in seen:
                seen.add(c.call_id)
                unique.append(c)
        return unique

    def tool_results(self) -> dict[str, Any]:
        """Map of call_id -> tool result as seen by the model."""
        results: dict[str, Any] = {}
        for call in self.calls:
            for content in call.function_results():
                results[content.call_id or ""] = content.result
        return results

    # -- MAF contract ----------------------------------------------------------
    def _next_message(self, messages: Sequence[Message]) -> tuple[Message, UsageDetails | None]:
        if not self._script:
            raise ScriptExhaustedError(
                f"Model call #{len(self.calls)} had no scripted turn. Last user text: "
                f"{self.calls[-1].last_user_text!r}"
            )
        item = self._script.pop(0)
        if callable(item) and not isinstance(item, (Turn, ToolCall, Message)):
            item = item(messages)
        if isinstance(item, Message):
            return item, None
        if isinstance(item, str):
            item = Turn(text=item)
        if isinstance(item, ToolCall):
            item = Turn(tool_calls=(item,))
        contents: list[Content] = []
        for tc in item.tool_calls:
            self._call_counter += 1
            contents.append(
                Content.from_function_call(
                    call_id=tc.call_id or f"call_{self._call_counter}",
                    name=tc.name,
                    arguments=json.dumps(tc.arguments),
                )
            )
        if item.text is not None:
            contents.append(Content.from_text(item.text))
        return Message(role="assistant", contents=contents), item.usage

    def _inner_get_response(self, *, messages: Sequence[Message], options: Any, stream: bool = False, **kwargs: Any):
        self.calls.append(RecordedCall(messages=list(messages), options=dict(options or {})))
        message, usage = self._next_message(messages)
        response_id = f"scripted-{len(self.calls)}"

        if stream:

            async def _updates():
                contents = list(message.contents)
                if usage:
                    contents.append(Content.from_usage(usage))
                yield ChatResponseUpdate(role="assistant", contents=contents, response_id=response_id)

            return self._build_response_stream(_updates(), response_format=(options or {}).get("response_format"))

        async def _response() -> ChatResponse:
            return ChatResponse(messages=[message], response_id=response_id, usage_details=usage)

        return _response()


class ScriptedChatClient(FunctionInvocationLayer, ChatMiddlewareLayer, ChatTelemetryLayer, _RawScriptedClient):
    """Scripted chat client with the full MAF layer stack.

    Example::

        client = ScriptedChatClient(script=[tool_call("lookup_order", order_id="A1"), reply("Shipped.")])
        agent = Agent(client, tools=[lookup_order])
        assert (await agent.run("where is A1?")).text == "Shipped."
    """

    OTEL_PROVIDER_NAME = "agentkit.scripted"
