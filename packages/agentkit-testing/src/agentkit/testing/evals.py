"""Eval cases as data: one YAML file drives offline (scripted) and live runs.

Offline mode replays each case's ``script`` through ``ScriptedChatClient`` so CI
proves the wiring (tools, guardrails, instructions) without a model. Live mode
ignores the script and runs the same inputs and expectations against the real
model behind the gateway.

Case file format::

    cases:
      - id: order-lookup
        input: Where is order A1001?
        script:                       # offline only
          - tool: lookup_order
            args: {order_id: A1001}
          - reply: Order A1001 shipped yesterday.
        expect:
          contains: [shipped]         # case-insensitive substrings of the reply
          not_contains: [refund]
          tools: [lookup_order]       # tools that must have been called
          blocked: false              # whether a guardrail must have refused

Human approvals (tools with ``approval_mode="always_require"``)::

      - id: big-refund-needs-approval
        input: Refund $500 on A1002.
        approve: true                 # decide every approval request (true/false); omit to stop at the pause
        script:
          - tool: issue_refund
            args: {order_id: A1002, amount: 500}
          - reply: Refunded $500.
        expect:
          approval_required: [issue_refund]   # these tools must have paused for a human
          tools: [issue_refund]               # and (after approval) actually run
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from agent_framework import AgentResponse, FunctionMiddleware, Message, SupportsAgentRun

from .scripted_client import ScriptedChatClient, ToolCall, Turn

__all__ = ["BLOCKED_KEY", "CaseResult", "EvalCase", "load_eval_cases", "run_case"]

#: Response ``additional_properties`` key set by agentkit guardrails when they refuse.
BLOCKED_KEY = "agentkit.blocked"


@dataclass
class EvalCase:
    id: str
    input: str
    expect: dict[str, Any] = field(default_factory=dict)
    script: list[Any] = field(default_factory=list)
    approve: bool | None = None

    def scripted_turns(self) -> list[Turn]:
        turns: list[Turn] = []
        for step in self.script:
            if "reply" in step:
                turns.append(Turn(text=str(step["reply"])))
            elif "tool" in step:
                turns.append(Turn(tool_calls=(ToolCall(name=step["tool"], arguments=dict(step.get("args") or {})),)))
            elif "tools" in step:
                calls = tuple(ToolCall(name=t["tool"], arguments=dict(t.get("args") or {})) for t in step["tools"])
                turns.append(Turn(tool_calls=calls))
            else:
                raise ValueError(f"case {self.id}: script step needs 'reply', 'tool' or 'tools': {step!r}")
        return turns


@dataclass
class CaseResult:
    case: EvalCase
    response: AgentResponse | None
    tools_called: list[str]
    failures: list[str]

    @property
    def passed(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        detail = "" if self.passed else " — " + "; ".join(self.failures)
        return f"[{status}] {self.case.id}{detail}"


def load_eval_cases(path: str | Path) -> list[EvalCase]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cases = []
    ids: set[str] = set()
    for raw in data.get("cases", []):
        case = EvalCase(
            id=str(raw["id"]),
            input=str(raw["input"]),
            expect=dict(raw.get("expect") or {}),
            script=list(raw.get("script") or []),
            approve=raw.get("approve"),
        )
        if case.id in ids:
            raise ValueError(f"duplicate eval case id: {case.id}")
        ids.add(case.id)
        cases.append(case)
    return cases


class _ToolRecorder(FunctionMiddleware):
    def __init__(self) -> None:
        self.names: list[str] = []

    async def process(self, context, call_next) -> None:  # type: ignore[override]
        self.names.append(context.function.name)
        await call_next()


AgentFactory = Callable[[ScriptedChatClient | None], SupportsAgentRun]


async def run_case(agent_factory: AgentFactory, case: EvalCase, *, live: bool = False) -> CaseResult:
    """Run one case. ``agent_factory(client)`` must build the agent; ``None`` means use the real client."""
    client = None if live else ScriptedChatClient(script=case.scripted_turns())
    agent = agent_factory(client)
    recorder = _ToolRecorder()
    failures: list[str] = []
    response: AgentResponse | None = None
    paused_for: list[str] = []
    try:
        session = agent.create_session() if hasattr(agent, "create_session") else None
        response = await agent.run(case.input, middleware=[recorder], session=session)
        rounds = 0
        while response.user_input_requests and rounds < 10:
            requests = [r for r in response.user_input_requests if r.type == "function_approval_request"]
            paused_for += [r.function_call.name for r in requests if r.function_call is not None]
            if case.approve is None or not requests:
                break
            decision = Message(role="user", contents=[r.to_function_approval_response(approved=bool(case.approve))
                                                      for r in requests])
            response = await agent.run(decision, middleware=[recorder], session=session)
            rounds += 1
    except Exception as exc:  # surface as a failed case, not a crashed suite
        failures.append(f"run raised {type(exc).__name__}: {exc}")
        return CaseResult(case, None, recorder.names, failures)

    text = (response.text or "").lower()
    expect = case.expect
    for needle in expect.get("contains", []):
        if str(needle).lower() not in text:
            failures.append(f"reply missing {needle!r}")
    for needle in expect.get("not_contains", []):
        if str(needle).lower() in text:
            failures.append(f"reply unexpectedly contains {needle!r}")
    for tool_name in expect.get("tools", []):
        if tool_name not in recorder.names:
            failures.append(f"tool {tool_name!r} not called (called: {recorder.names})")
    for tool_name in expect.get("forbidden_tools", []):
        if tool_name in recorder.names:
            failures.append(f"forbidden tool {tool_name!r} was called")
    for tool_name in expect.get("approval_required", []):
        if tool_name not in paused_for:
            failures.append(f"tool {tool_name!r} did not pause for approval (paused: {paused_for})")
    if expect.get("approval_required") == [] and paused_for:
        failures.append(f"unexpected approval pause for {paused_for}")
    if "blocked" in expect:
        blocked = bool((response.additional_properties or {}).get(BLOCKED_KEY))
        if blocked != bool(expect["blocked"]):
            failures.append(f"expected blocked={expect['blocked']} but was {blocked}")
    stopped_at_pause = bool(paused_for) and case.approve is None
    if client is not None and not failures and not stopped_at_pause:
        try:
            client.assert_script_consumed()
        except AssertionError as exc:
            failures.append(str(exc))
    return CaseResult(case, response, recorder.names, failures)


async def run_suite(
    agent_factory: AgentFactory, cases: list[EvalCase], *, live: bool = False
) -> list[CaseResult]:
    return [await run_case(agent_factory, c, live=live) for c in cases]
