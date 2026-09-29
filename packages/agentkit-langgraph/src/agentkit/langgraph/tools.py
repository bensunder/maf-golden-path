"""Tools for LangGraph agents, governed by the same function middleware as MAF agents.

Teams write tools once, with MAF's ``@tool`` (``approval_mode="always_require"`` for risky ones), and both
kinds of agent use them. For a LangGraph agent each tool becomes a LangChain tool whose every call runs:

1. the agent's function middleware, outermost first: :class:`~agentkit.guardrails.ToolPolicyMiddleware`
   (deny/allow lists, argument validators) and :class:`~agentkit.guardrails.ToolOutputShieldMiddleware`
   (indirect prompt injection in the result), the very same objects a MAF agent uses;
2. then, for tools that need a person, a LangGraph ``interrupt``: the run pauses, the host shows the
   approval (web chat, console, Teams) and resumes the graph with the decision. Denied calls never ask.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from agent_framework import Content, FunctionInvocationContext, FunctionTool
from langchain_core.tools import StructuredTool
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt
from opentelemetry import trace

__all__ = ["APPROVAL_INTERRUPT", "DECIDED_KEY", "DONE_KEY", "govern_tools", "new_run", "result_text"]

APPROVAL_INTERRUPT = "agentkit.approval"  # the ``type`` of the interrupt value a governed tool raises
DONE_KEY = "agentkit.langgraph.done"  # session state: governed calls finished in this turn (key -> result)
DECIDED_KEY = "agentkit.langgraph.decided"  # session state: the person's decisions in this turn (key -> approved)
_tracer = trace.get_tracer("agentkit.langgraph")


class _Run:
    """One graph run (a turn or a resume): how many identical calls each task made, and the calls still running."""

    def __init__(self) -> None:
        self.counts: dict[tuple, int] = {}
        self.running: set[asyncio.Task[Any]] = set()

    async def settle(self) -> None:
        """Wait for governed calls still running. A node that runs calls concurrently (``asyncio.gather``) and is
        paused by one of them leaves its siblings running; they must finish and be recorded as done before the
        session is saved, or the resumed node would run them a second time."""
        while self.running:
            await asyncio.gather(*list(self.running), return_exceptions=True)


_RUN: contextvars.ContextVar[_Run | None] = contextvars.ContextVar("agentkit_lg_run", default=None)


def new_run() -> _Run:
    """Called at the start of every graph run (a turn or a resume): calls are numbered afresh, so a resumed node
    gives each call the same key it had before. ``await run.settle()`` before saving the session."""
    run = _Run()
    _RUN.set(run)
    return run


def _current_run() -> _Run:
    run = _RUN.get()
    if run is None:
        run = new_run()
    return run


def _call_key(tool: str, args: dict[str, Any]) -> str:
    """Which call this is: the graph task and step it runs in, the tool, its arguments, and how many identical
    calls came before it in that task. Stable when a node re-runs on resume, different for every distinct call."""
    try:
        from langgraph.config import get_config

        meta = get_config().get("metadata") or {}
    except Exception:  # called outside a graph
        meta = {}
    place = (str(meta.get("langgraph_checkpoint_ns", "")), str(meta.get("langgraph_step", "")))
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    counts = _current_run().counts
    n = counts.get((place, tool, canonical), 0)
    counts[(place, tool, canonical)] = n + 1
    return hashlib.sha256(json.dumps([*place, tool, canonical, n]).encode()).hexdigest()[:32]


def _pause(value: dict[str, Any]) -> None:
    from langgraph.config import get_config

    ns = get_config().get("configurable", {}).get("checkpoint_ns", "")
    raise GraphInterrupt((Interrupt.from_ns(value=value, ns=ns),))


def result_text(result: Any) -> str:
    """What the model reads back from a tool result."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, Content):
        return result.text or str(result.result if result.type == "function_result" else "") or ""
    if isinstance(result, Iterable) and not isinstance(result, (bytes, dict)):
        return "\n".join(t for t in (result_text(r) for r in result) if t)
    return str(result)


def _needs_person(tool: FunctionTool, args: dict[str, Any], rules: Sequence[Callable[[Content], bool]]) -> bool:
    if tool.approval_mode != "always_require":
        return False
    call = Content.from_function_call(call_id="rule-check", name=tool.name, arguments=args)
    return not any(_safe(rule, call) for rule in rules)


def _safe(rule: Callable[[Content], bool], call: Content) -> bool:
    try:
        return bool(rule(call))
    except Exception:  # a broken rule must never auto-approve
        return False


def _governed(tool: FunctionTool, function_middleware: Sequence[Any], rules: Sequence[Callable[[Content], bool]],
              session: Any) -> StructuredTool:
    async def run(**args: Any) -> str:
        task = asyncio.current_task()
        running = _current_run().running
        if task is not None:
            running.add(task)
        try:
            return await _run(**args)
        finally:
            if task is not None:
                running.discard(task)

    async def _run(**args: Any) -> str:
        state = session.state if session is not None else {}
        done: dict[str, str] = state.setdefault(DONE_KEY, {})
        decided: dict[str, bool] = state.setdefault(DECIDED_KEY, {})
        key = _call_key(tool.name, dict(args))
        if key in done:  # a node re-running on resume: this call already ran (or was refused) this turn
            return done[key]
        context = FunctionInvocationContext(function=tool, arguments=dict(args), session=session)

        async def execute() -> None:
            if _needs_person(tool, dict(args), rules):
                if key not in decided:
                    # pause the graph; the person decides this exact call (the approval's id is this call's key).
                    # Raised directly, not with ``interrupt()``: that hands back resume values by position within
                    # the node, so a second gated call in a re-run node would take the first call's decision.
                    _pause({"type": APPROVAL_INTERRUPT, "tool": tool.name, "arguments": dict(args), "key": key})
                if decided.get(key) is not True:
                    context.result = f"The person didn't approve {tool.name}, so it was not run."
                    return
            context.result = await tool.invoke(arguments=dict(args))

        async def step(i: int) -> None:
            if i == len(function_middleware):
                await execute()
                return
            await function_middleware[i].process(context, lambda: step(i + 1))

        # a pause for approval (LangGraph's interrupt) passes through here as an exception: it isn't an error
        with _tracer.start_as_current_span(f"execute_tool {tool.name}", record_exception=False,
                                           set_status_on_exception=False, attributes={
                "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool.name, "langsmith.span.kind": "tool"}):
            await step(0)
        done[key] = result_text(context.result)
        return done[key]

    return StructuredTool.from_function(coroutine=run, name=tool.name, description=tool.description or tool.name,
                                        args_schema=tool.parameters())


def govern_tools(
    tools: Sequence[FunctionTool],
    *,
    function_middleware: Sequence[Any] = (),
    approval_rules: Sequence[Callable[[Content], bool]] = (),
    session: Any = None,
) -> list[StructuredTool]:
    """LangChain tools for LangGraph, each call governed like a MAF tool call (see the module docstring)."""
    return [_governed(t, list(function_middleware), list(approval_rules), session) for t in tools]
