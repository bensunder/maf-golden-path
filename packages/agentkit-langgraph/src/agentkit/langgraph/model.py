"""A LangChain chat model over the kit's MAF chat client.

LangGraph nodes call models through LangChain's ``BaseChatModel`` interface. :class:`MafChatModel` puts the
agent's own MAF client behind that interface, so a LangGraph agent's model calls take exactly the paved
road: the AI gateway with Entra auth and the ``x-agentkit-*`` headers, retries, GenAI telemetry spans, and
the agent's chat middleware (PII redaction) — and tests use the same ``ScriptedChatClient`` scripts.

The client is called *below* its function-invocation layer: tools are declared to the model, but LangGraph
(not MAF) decides what runs, through :func:`agentkit.langgraph.govern_tools`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from agent_framework import Content, FunctionInvocationLayer, FunctionTool, Message
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict

__all__ = ["MafChatModel", "to_maf_messages"]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content blocks
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content or "")


def to_maf_messages(messages: Sequence[BaseMessage]) -> list[Message]:
    out: list[Message] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append(Message(role="system", contents=[_text(m.content)]))
        elif isinstance(m, HumanMessage):
            out.append(Message(role="user", contents=[_text(m.content)]))
        elif isinstance(m, AIMessage):
            contents: list[Any] = []
            if _text(m.content):
                contents.append(_text(m.content))
            for call in m.tool_calls:
                contents.append(Content.from_function_call(call_id=call["id"] or "", name=call["name"],
                                                           arguments=dict(call.get("args") or {})))
            out.append(Message(role="assistant", contents=contents or [""]))
        elif isinstance(m, ToolMessage):
            out.append(Message(role="tool", contents=[Content.from_function_result(call_id=m.tool_call_id,
                                                                                    result=_text(m.content))]))
        else:  # anything else is shown to the model as user text
            out.append(Message(role="user", contents=[_text(m.content)]))
    return out


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except ValueError:
            return {"_raw": value}
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    return dict(value or {})


class MafChatModel(BaseChatModel):
    """``BaseChatModel`` over a MAF chat client (``create_chat_client`` or ``ScriptedChatClient``)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: Any
    chat_middleware: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "agentkit-maf"

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any):  # type: ignore[override]
        formatted = [convert_to_openai_tool(t) for t in tools]
        return self.bind(tools=formatted, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # pragma: no cover - LangGraph runs async
        raise NotImplementedError("MafChatModel is async: use ainvoke / astream (LangGraph does)")

    async def _agenerate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        declared = [
            FunctionTool(name=t["function"]["name"], description=t["function"].get("description", ""),
                         func=None, input_model=t["function"].get("parameters") or {"type": "object", "properties": {}})
            for t in kwargs.get("tools") or []
        ]
        # One tool call per model step: every call then runs, pauses for approval and resumes on its own, so an
        # approval can never be applied to a sibling call, and a resumed step never runs a finished call again.
        options: dict[str, Any] = {"tools": declared, "allow_multiple_tool_calls": False} if declared else {}
        # like a MAF agent: leading system messages go to the client as its instructions option
        while messages and isinstance(messages[0], SystemMessage):
            options["instructions"] = "\n\n".join(p for p in (options.get("instructions"), _text(messages[0].content)) if p)
            messages = messages[1:]
        client_kwargs = {"middleware": list(self.chat_middleware)} if self.chat_middleware else None
        if isinstance(self.client, FunctionInvocationLayer):
            # Below the function-invocation layer: the model's tool calls come back to LangGraph unexecuted.
            get_response = super(FunctionInvocationLayer, self.client).get_response
        else:
            get_response = self.client.get_response
        response = await get_response(to_maf_messages(messages), options=options, client_kwargs=client_kwargs)

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for message in response.messages:
            for content in message.contents:
                if content.type == "text" and content.text:
                    text_parts.append(content.text)
                elif content.type == "function_call":
                    tool_calls.append({"id": content.call_id, "name": content.name,
                                       "args": _arguments(content.arguments), "type": "tool_call"})
        usage = dict(response.usage_details or {})
        metadata = None
        if usage:
            inp, outp = int(usage.get("input_token_count") or 0), int(usage.get("output_token_count") or 0)
            metadata = {"input_tokens": inp, "output_tokens": outp,
                        "total_tokens": int(usage.get("total_token_count") or inp + outp)}
        # ...even if the model ignores the parallel-calls setting: later calls are left for later steps
        message = AIMessage(content="".join(text_parts), tool_calls=tool_calls[:1], usage_metadata=metadata)
        return ChatResult(generations=[ChatGeneration(message=message)])
