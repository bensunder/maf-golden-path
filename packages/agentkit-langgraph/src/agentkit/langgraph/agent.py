"""LangGraph agents that run on the agentkit host, with the same guardrails as MAF agents.

:class:`LangGraphAgent` is a MAF agent (``AgentMiddlewareLayer`` over ``BaseAgent``) whose run executes a
LangGraph graph. Everything the host does for a MAF agent therefore applies unchanged: sessions and
cross-replica locking, human approvals and their audit trail, the web chat, Teams, the console, telemetry,
evals and the deploy gate. Inside a run:

* agent middleware (input guard, token budget, run metrics) wraps the whole graph run;
* chat middleware (PII redaction) wraps every model call, made through the kit's own client
  (:class:`~agentkit.langgraph.MafChatModel`);
* function middleware (tool policy, tool-output shield) wraps every tool call, and tools that need a person
  pause the graph (a LangGraph interrupt) until someone decides (:func:`~agentkit.langgraph.govern_tools`).

    from agentkit.langgraph import build_graph_agent

    agent = build_graph_agent(name="claims", instructions=INSTRUCTIONS, tools=[lookup_claim, pay_claim],
                              graph=my_graph)   # optional: default is a tool-calling (ReAct) loop
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from typing import Any

from agent_framework import (
    AgentMiddlewareLayer,
    AgentResponse,
    AgentResponseUpdate,
    AgentSession,
    BaseAgent,
    ChatMiddleware,
    Content,
    FunctionMiddleware,
    FunctionTool,
    Message,
    ResponseStream,
    UsageDetails,
    normalize_messages,
)
from agent_framework import tool as as_tool
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command
from opentelemetry import trace

from .checkpoint import STATE_KEY, load_saver, save_saver
from .model import MafChatModel
from .tools import APPROVAL_INTERRUPT, DECIDED_KEY, DONE_KEY, govern_tools, new_run

__all__ = ["GraphFactory", "LangGraphAgent", "build_graph_agent", "react_graph"]

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("agentkit.langgraph")

#: ``graph(model, tools, instructions) -> StateGraph`` (uncompiled; the agent compiles it with its checkpointer).
GraphFactory = Callable[[MafChatModel, list[Any], str], StateGraph]


def react_graph(model: MafChatModel, tools: list[Any], instructions: str) -> StateGraph:
    """The default graph: the model calls tools until it can answer (a ReAct loop)."""
    bound = model.bind_tools(tools) if tools else model

    async def call_model(state: MessagesState) -> dict[str, list[BaseMessage]]:
        return {"messages": [await bound.ainvoke([SystemMessage(instructions), *state["messages"]])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    graph.add_edge(START, "agent")
    if tools:
        graph.add_node("tools", ToolNode(tools))
        graph.add_conditional_edges("agent", tools_condition)
        graph.add_edge("tools", "agent")
    else:
        graph.add_edge("agent", END)
    return graph


def _mcp_like(tool: Any) -> bool:
    return hasattr(tool, "functions") and hasattr(tool, "is_connected") and hasattr(tool, "_prepare_for_run")


class _RawLangGraphAgent(BaseAgent):
    def __init__(self, *, client: Any, instructions: str, tools: Sequence[Any], graph: GraphFactory | None,
                 approval_rules: Sequence[Callable[[Content], bool]] = (), checkpoint_types: Sequence[type] = (),
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.client = client
        self.instructions = instructions
        self.approval_rules = list(approval_rules)
        self.checkpoint_types = list(checkpoint_types)
        self.graph_factory: GraphFactory = graph or react_graph
        self.mcp_tools = [t for t in tools if _mcp_like(t)]
        self.tools: list[FunctionTool] = [t if isinstance(t, FunctionTool) else as_tool(t)
                                          for t in tools if not _mcp_like(t)]
        self._exit_stack = AsyncExitStack()

    async def _all_tools(self) -> list[FunctionTool]:
        tools = list(self.tools)
        for server in self.mcp_tools:  # platform connectors: connected like a MAF agent connects them
            await server._prepare_for_run({})
            if not server.is_connected:
                await self._exit_stack.enter_async_context(server)
                await server._prepare_for_run({})
            tools.extend(server.functions)
        return tools

    async def _execute(self, messages: list[Message], session: AgentSession | None,
                       client_kwargs: dict[str, Any] | None) -> AgentResponse:
        session = session or self.create_session()
        middleware = list((client_kwargs or {}).get("middleware") or [])
        chat_mw = [m for m in middleware if isinstance(m, ChatMiddleware)]
        function_mw = [m for m in middleware if isinstance(m, FunctionMiddleware)]

        model = MafChatModel(client=self.client, chat_middleware=chat_mw)
        tools = govern_tools(await self._all_tools(), function_middleware=function_mw,
                             approval_rules=self.approval_rules, session=session)
        thread_id = session.session_id or "default"
        saver = load_saver(session.state.get(STATE_KEY), thread_id, extra_types=self.checkpoint_types)
        graph = self.graph_factory(model, tools, self.instructions).compile(checkpointer=saver)
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}

        decisions = {c.id: bool(c.approved) for m in messages for c in m.contents
                     if c.type == "function_approval_response" and c.id}
        if decisions:
            # Each decision is recorded against the exact call it was for (the approval id is the call's key); the
            # governed tools read them there, so it doesn't matter in what order LangGraph resumes interrupts.
            session.state.setdefault(DECIDED_KEY, {}).update(decisions)
            pending = (await graph.aget_state(config)).interrupts
            graph_input: Any = Command(resume={i.id: {"decisions": decisions} for i in pending})
        else:
            session.state.pop(DONE_KEY, None)  # a new turn: nothing from the last one counts as done or decided
            session.state.pop(DECIDED_KEY, None)
            text = "\n".join(m.text for m in messages if m.role == "user" and m.text)
            graph_input = {"messages": [HumanMessage(text)]}
        run = new_run()

        before = len((await graph.aget_state(config)).values.get("messages", [])) if not decisions else None
        # the agent-level span MAF's own agents get (their model and tool calls nest under it)
        with _tracer.start_as_current_span(f"invoke_agent {self.name}", attributes={
                "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": self.name or "",
                "gen_ai.agent.id": self.id or "", "agentkit.framework": "langgraph", "langsmith.span.kind": "chain"}) as span:
            result = await graph.ainvoke(graph_input, config)
            await run.settle()
            if result.get("__interrupt__"):
                span.set_attribute("agentkit.approval_required", True)
        if not result.get("__interrupt__"):
            session.state.pop(DONE_KEY, None)
            session.state.pop(DECIDED_KEY, None)
        session.state[STATE_KEY] = save_saver(saver, thread_id)
        return self._response(result, before)

    @staticmethod
    def _response(result: dict[str, Any], before: int | None) -> AgentResponse:
        all_messages: list[BaseMessage] = result.get("messages", [])
        new = all_messages[before:] if before is not None else all_messages
        # the answer: the last model message after the newest human turn
        answer = next((m for m in reversed(all_messages) if isinstance(m, AIMessage)), None)
        contents: list[Any] = []
        interrupts = result.get("__interrupt__") or []
        for item in interrupts:
            value = item.value if isinstance(item.value, dict) else {}
            if value.get("type") != APPROVAL_INTERRUPT:
                continue
            approval_id = str(value.get("key") or item.id)  # the call's own key: unique per call, stable on resume
            call = Content.from_function_call(call_id=approval_id, name=value["tool"], arguments=value.get("arguments") or {},
                                              id=approval_id)
            contents.append(Content.from_function_approval_request(id=approval_id, function_call=call))
        if not interrupts and answer is not None and answer.content:
            contents.insert(0, answer.content if isinstance(answer.content, str) else str(answer.content))
        usage = UsageDetails()
        seen = new if before is not None else [answer] if answer else []
        for m in seen:
            meta = getattr(m, "usage_metadata", None) or {}
            if meta:
                usage["input_token_count"] = (usage.get("input_token_count") or 0) + int(meta.get("input_tokens", 0))
                usage["output_token_count"] = (usage.get("output_token_count") or 0) + int(meta.get("output_tokens", 0))
                usage["total_token_count"] = (usage.get("total_token_count") or 0) + int(meta.get("total_tokens", 0))
        return AgentResponse(messages=[Message(role="assistant", contents=contents or [""])],
                             response_id=str(uuid.uuid4()), usage_details=usage or None)

    def run(self, messages: Any = None, *, stream: bool = False, session: AgentSession | None = None,
            tools: Any = None, options: Any = None, client_kwargs: Any = None, **kwargs: Any):
        normalized = normalize_messages(messages)
        if not stream:
            return self._execute(normalized, session, client_kwargs)
        holder: dict[str, AgentResponse] = {}

        async def updates():
            response = await self._execute(normalized, session, client_kwargs)
            holder["response"] = response
            for message in response.messages:
                yield AgentResponseUpdate(contents=list(message.contents), role="assistant",
                                          response_id=response.response_id)

        return ResponseStream(updates(), finalizer=lambda _updates: holder["response"])

    async def close(self) -> None:
        await self._exit_stack.aclose()


class LangGraphAgent(AgentMiddlewareLayer, _RawLangGraphAgent):
    """A LangGraph graph as a MAF agent, so it runs under the kit's host with the agent's middleware."""


def build_graph_agent(
    *,
    name: str,
    instructions: str,
    tools: Sequence[Any] = (),
    graph: GraphFactory | None = None,
    settings: Any = None,
    client: Any = None,
    detector: Any = None,
    tool_policy: Any = None,
    extra_middleware: Sequence[Any] = (),
    approval_rules: Sequence[Callable[[Content], bool]] = (),
    description: str | None = None,
    checkpoint_types: Sequence[type] = (),
) -> LangGraphAgent:
    """Like :func:`agentkit.hosting.build_agent`, for a LangGraph graph: same settings, same middleware stack,
    same platform connectors and peer agents, same model client (the AI gateway, or a scripted one in tests).

    ``checkpoint_types`` are your own classes in the graph's state (beyond messages), allowed when a paused graph
    is read back from the session store; nothing else is ever reconstructed from stored data."""
    from collections.abc import Mapping

    from agentkit.guardrails import ToolPolicyMiddleware
    from agentkit.hosting import AgentKitSettings, default_middleware
    from agentkit.hosting.agent import _platform_connectors, _platform_peers
    from agentkit.hosting.clients import create_chat_client

    settings = settings or AgentKitSettings()
    tools = [*tools, *_platform_connectors(settings), *_platform_peers(settings)]
    if isinstance(tool_policy, Mapping):
        tool_policy = ToolPolicyMiddleware(**tool_policy)
    client = client or create_chat_client(settings, agent_name=name)
    # Approval rules are applied by the governed tools (before the graph pauses), so MAF's approval middleware
    # isn't in this stack.
    middleware = [*default_middleware(settings, agent_name=name, detector=detector, tool_policy=tool_policy),
                  *extra_middleware]
    return LangGraphAgent(client=client, instructions=instructions, tools=tools, graph=graph,
                          approval_rules=approval_rules, checkpoint_types=checkpoint_types, name=name, id=name,
                          description=description,
                          middleware=middleware,
                          additional_properties={"agentkit.service_version": settings.service_version,
                                                 "agentkit.framework": "langgraph"})
