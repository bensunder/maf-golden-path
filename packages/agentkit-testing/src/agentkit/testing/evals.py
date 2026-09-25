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

Quality checks (scored by an LLM judge in live runs; skipped offline)::

      - id: tracking-answer
        input: Where is A1001?
        critical: false               # critical cases must pass on every repetition in the gate
        expect:
          tool_args: {lookup_order: {order_id: A1001}}   # a call with at least these arguments
          max_tool_calls: 3
          max_total_tokens: 4000
          rubric: States the order status and tracking number; no invented delivery date.
          min_score: 4                # 1-5, default 4
          grounded: true              # every claim supported by tool results (judge)

Knowledge (documents searched as a user, with citations)::

      - id: support-agent-cant-see-lead-playbook
        input: Can I approve a $300 refund exception?
        user: sam                     # run as this user: search is trimmed to what they may read
        critical: true
        expect:
          cites: [refund-policy]      # the answer cites these documents (and no [n] that wasn't retrieved)
          must_not_retrieve: [leads-playbook]   # never shown to this user, whatever the answer says
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from agent_framework import AgentResponse, FunctionMiddleware, Message, SupportsAgentRun

from .judge import GROUNDEDNESS_RUBRIC, Judge, JudgeScore
from .scripted_client import ScriptedChatClient, ToolCall, Turn

__all__ = ["BLOCKED_KEY", "CaseResult", "EvalCase", "RecordedToolCall", "load_eval_cases", "run_case", "run_suite"]

#: Response ``additional_properties`` key set by agentkit guardrails when they refuse.
BLOCKED_KEY = "agentkit.blocked"


@dataclass
class EvalCase:
    id: str
    input: str
    expect: dict[str, Any] = field(default_factory=dict)
    script: list[Any] = field(default_factory=list)
    approve: bool | None = None
    critical: bool = False
    repeat: int | None = None
    user: str | None = None

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

    @property
    def needs_judge(self) -> bool:
        return bool(self.expect.get("rubric") or self.expect.get("grounded"))


@dataclass
class RecordedToolCall:
    name: str
    arguments: dict[str, Any]
    result: str


@dataclass
class CaseResult:
    case: EvalCase
    response: AgentResponse | None
    tool_calls: list[RecordedToolCall]
    failures: list[str]
    scores: list[JudgeScore] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    total_tokens: int = 0

    @property
    def tools_called(self) -> list[str]:
        return [c.name for c in self.tool_calls]

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
    known = {"contains", "not_contains", "tools", "forbidden_tools", "blocked", "approval_required", "tool_args",
             "max_tool_calls", "max_total_tokens", "rubric", "min_score", "grounded", "grounded_min_score",
             "cites", "must_not_retrieve"}
    for raw in data.get("cases", []):
        case = EvalCase(
            id=str(raw["id"]),
            input=str(raw["input"]),
            expect=dict(raw.get("expect") or {}),
            script=list(raw.get("script") or []),
            approve=raw.get("approve"),
            critical=bool(raw.get("critical", False)),
            repeat=raw.get("repeat"),
            user=str(raw["user"]) if raw.get("user") is not None else None,
        )
        unknown = set(case.expect) - known
        if unknown:  # typos in expectations would otherwise silently check nothing
            raise ValueError(f"case {case.id}: unknown expect keys {sorted(unknown)}; allowed: {sorted(known)}")
        if case.id in ids:
            raise ValueError(f"duplicate eval case id: {case.id}")
        ids.add(case.id)
        cases.append(case)
    return cases


class _ToolRecorder(FunctionMiddleware):
    def __init__(self) -> None:
        self.calls: list[RecordedToolCall] = []

    async def process(self, context, call_next) -> None:  # type: ignore[override]
        args = context.arguments
        args = args.model_dump() if hasattr(args, "model_dump") else dict(args or {})
        await call_next()
        result = context.result
        if isinstance(result, list):
            result = "\n".join(getattr(r, "text", None) or str(getattr(r, "result", "") or r) for r in result)
        self.calls.append(RecordedToolCall(context.function.name, args, str(result)[:4000]))


# Knowledge tool results use agentkit's shared source format (defined in agentkit.hosting.citations;
# parsed again here so this package stays dependency-free). A test in agentkit-knowledge keeps them in step.
_SOURCE_LINE = re.compile(r"^\[(\d+)\] id=(\S+) \u00b7 title=")
_MARKER = re.compile(r"\[(\d+)\]")


def _retrieved(calls: list[RecordedToolCall]) -> dict[int, str]:
    """[n] -> document id, for every source any tool showed the model."""
    found: dict[int, str] = {}
    for call in calls:
        for line in call.result.splitlines():
            if m := _SOURCE_LINE.match(line.strip()):
                found.setdefault(int(m.group(1)), m.group(2))
    return found


def _as_user(user: str | None, case_id: str):
    if user is None:
        return contextlib.nullcontext()
    try:
        from agentkit.telemetry import run_context
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("eval cases with 'user:' need agentkit-telemetry installed") from exc
    return run_context(user_id=user, request_id=f"eval-{case_id}-{uuid.uuid4().hex[:8]}")


def _same(actual: Any, expected: Any) -> bool:
    if isinstance(expected, str) and isinstance(actual, str):
        return actual.strip().lower() == expected.strip().lower()
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(actual) - float(expected)) < 1e-9
    return actual == expected


def _tokens(response: AgentResponse | None) -> int:
    usage = (response.usage_details if response else None) or {}
    return int(usage.get("total_token_count") or (usage.get("input_token_count") or 0) + (usage.get("output_token_count") or 0))


AgentFactory = Callable[[ScriptedChatClient | None], SupportsAgentRun]


async def run_case(
    agent_factory: AgentFactory, case: EvalCase, *, live: bool = False, judge: Judge | None = None
) -> CaseResult:
    """Run one case. ``agent_factory(client)`` builds the agent; ``None`` means use the real client.

    Deterministic checks always run. Judge checks (``rubric``, ``grounded``) run when ``judge`` is given
    and are otherwise recorded as skipped (offline CI)."""
    client = None if live else ScriptedChatClient(script=case.scripted_turns())
    agent = agent_factory(client)
    recorder = _ToolRecorder()
    failures: list[str] = []
    response: AgentResponse | None = None
    paused_for: list[str] = []
    total_tokens = 0
    try:
        user_scope = _as_user(case.user, case.id)
        user_scope.__enter__()
    except Exception as exc:
        return CaseResult(case, None, [], [f"run raised {type(exc).__name__}: {exc}"])
    try:
        session = agent.create_session() if hasattr(agent, "create_session") else None
        response = await agent.run(case.input, middleware=[recorder], session=session)
        total_tokens += _tokens(response)
        rounds = 0
        while response.user_input_requests and rounds < 10:
            requests = [r for r in response.user_input_requests if r.type == "function_approval_request"]
            paused_for += [r.function_call.name for r in requests if r.function_call is not None]
            if case.approve is None or not requests:
                break
            decision = Message(role="user", contents=[r.to_function_approval_response(approved=bool(case.approve))
                                                      for r in requests])
            response = await agent.run(decision, middleware=[recorder], session=session)
            total_tokens += _tokens(response)
            rounds += 1
    except Exception as exc:  # surface as a failed case, not a crashed suite
        failures.append(f"run raised {type(exc).__name__}: {exc}")
        return CaseResult(case, None, recorder.calls, failures, total_tokens=total_tokens)
    finally:
        user_scope.__exit__(None, None, None)

    reply = response.text or ""
    text = reply.lower()
    called = [c.name for c in recorder.calls]
    expect = case.expect
    for needle in expect.get("contains", []):
        if str(needle).lower() not in text:
            failures.append(f"reply missing {needle!r}")
    for needle in expect.get("not_contains", []):
        if str(needle).lower() in text:
            failures.append(f"reply unexpectedly contains {needle!r}")
    for tool_name in expect.get("tools", []):
        if tool_name not in called:
            failures.append(f"tool {tool_name!r} not called (called: {called})")
    for tool_name in expect.get("forbidden_tools", []):
        if tool_name in called:
            failures.append(f"forbidden tool {tool_name!r} was called")
    for tool_name, wanted in (expect.get("tool_args") or {}).items():
        calls = [c for c in recorder.calls if c.name == tool_name]
        if not any(all(k in c.arguments and _same(c.arguments[k], v) for k, v in wanted.items()) for c in calls):
            seen = [c.arguments for c in calls] or "never called"
            failures.append(f"no {tool_name!r} call with arguments {wanted} (got: {seen})")
    if "max_tool_calls" in expect and len(called) > int(expect["max_tool_calls"]):
        failures.append(f"{len(called)} tool calls > max_tool_calls {expect['max_tool_calls']}")
    if "max_total_tokens" in expect and total_tokens > int(expect["max_total_tokens"]):
        failures.append(f"{total_tokens} tokens > max_total_tokens {expect['max_total_tokens']}")
    for tool_name in expect.get("approval_required", []):
        if tool_name not in paused_for:
            failures.append(f"tool {tool_name!r} did not pause for approval (paused: {paused_for})")
    if expect.get("approval_required") == [] and paused_for:
        failures.append(f"unexpected approval pause for {paused_for}")
    retrieved = _retrieved(recorder.calls)
    for doc_id in expect.get("must_not_retrieve", []):
        leaked = sorted(n for n, d in retrieved.items() if d == doc_id)
        if leaked:
            failures.append(f"retrieved {doc_id!r}, which this user must not see (as [{leaked[0]}])")
    if "cites" in expect:
        numbers = [int(n) for n in _MARKER.findall(reply)]
        bogus = sorted({n for n in numbers if n not in retrieved})
        if bogus:
            failures.append(f"cites {['[%d]' % n for n in bogus]} but no such source was retrieved")
        cited_docs = {retrieved[n] for n in numbers if n in retrieved}
        for doc_id in expect["cites"]:
            if doc_id not in cited_docs:
                failures.append(f"answer doesn't cite {doc_id!r} (cited: {sorted(cited_docs) or 'nothing'})")
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

    scores: list[JudgeScore] = []
    skipped: list[str] = []
    wanted_judgements = []
    if expect.get("rubric"):
        wanted_judgements.append(("rubric", str(expect["rubric"]), None, int(expect.get("min_score", 4))))
    if expect.get("grounded"):
        context = "\n".join(f"{c.name}({c.arguments}) -> {c.result}" for c in recorder.calls)
        wanted_judgements.append(("grounded", GROUNDEDNESS_RUBRIC, context, int(expect.get("grounded_min_score", 4))))
    for name, rubric, context, threshold in wanted_judgements:
        if judge is None:
            skipped.append(f"{name} (no judge)")
            continue
        score = await judge.score(name, rubric, query=case.input, response=reply, context=context, threshold=threshold)
        scores.append(score)
        if not score.passed:
            failures.append(f"{name} scored {score.score if score.score is not None else 'n/a'} < {threshold}: {score.reason}")
    return CaseResult(case, response, recorder.calls, failures, scores, skipped, total_tokens)


async def run_suite(
    agent_factory: AgentFactory, cases: list[EvalCase], *, live: bool = False, judge: Judge | None = None
) -> list[CaseResult]:
    return [await run_case(agent_factory, c, live=live, judge=judge) for c in cases]
