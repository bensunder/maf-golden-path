import pytest
from agent_framework import Agent, tool

from agentkit.testing import (
    ScriptedChatClient,
    ScriptExhaustedError,
    load_eval_cases,
    reply,
    run_case,
    tool_call,
)


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order."""
    return f"order {order_id}: shipped"


async def test_plain_reply():
    client = ScriptedChatClient(script=[reply("hello")])
    agent = Agent(client, instructions="be nice")
    result = await agent.run("hi")
    assert result.text == "hello"
    assert client.calls[0].last_user_text == "hi"
    assert client.calls[0].instructions == "be nice"
    client.assert_script_consumed()


async def test_tool_round_trip_reaches_model():
    client = ScriptedChatClient(script=[tool_call("lookup_order", order_id="A1"), reply("It shipped.")])
    agent = Agent(client, tools=[lookup_order])
    result = await agent.run("where is A1?")
    assert result.text == "It shipped."
    assert [c.name for c in client.tool_calls_made()] == ["lookup_order"]
    assert client.tool_calls_made()[0].arguments == {"order_id": "A1"}
    results = list(client.tool_results().values())
    assert "shipped" in str(results[0])


async def test_streaming():
    client = ScriptedChatClient(script=[reply("streamed")])
    agent = Agent(client)
    chunks = [u.text async for u in agent.run("hi", stream=True)]
    assert "".join(chunks) == "streamed"


async def test_usage_is_reported():
    client = ScriptedChatClient(script=[reply("ok", input_tokens=10, output_tokens=5)])
    result = await Agent(client).run("hi")
    assert result.usage_details["total_token_count"] == 15


async def test_callable_turn_sees_messages():
    client = ScriptedChatClient(script=[lambda msgs: f"echo:{msgs[-1].text}"])
    result = await Agent(client).run("ping")
    assert result.text == "echo:ping"


async def test_exhausted_script_fails_loudly():
    client = ScriptedChatClient(script=[])
    with pytest.raises(ScriptExhaustedError):
        await Agent(client).run("hi")


async def test_span_recorder_captures_agent_spans(span_recorder):
    client = ScriptedChatClient(script=[reply("ok")])
    await Agent(client, name="probe").run("hi")
    assert any("probe" in n or "invoke_agent" in n for n in span_recorder.names()), span_recorder.names()


async def test_eval_case_runner(tmp_path):
    cases_file = tmp_path / "cases.yaml"
    cases_file.write_text(
        """
cases:
  - id: ok
    input: where is A1?
    script:
      - tool: lookup_order
        args: {order_id: A1}
      - reply: Order A1 shipped.
    expect: {contains: [shipped], tools: [lookup_order], blocked: false}
  - id: wrong
    input: where is A1?
    script:
      - reply: No idea.
    expect: {contains: [shipped], tools: [lookup_order]}
"""
    )
    cases = load_eval_cases(cases_file)
    factory = lambda client: Agent(client, tools=[lookup_order])  # noqa: E731
    ok = await run_case(factory, cases[0])
    bad = await run_case(factory, cases[1])
    assert ok.passed, ok.summary()
    assert not bad.passed
    assert any("shipped" in f for f in bad.failures)
    assert any("lookup_order" in f for f in bad.failures)


async def test_eval_cases_with_approvals(tmp_path):
    from agent_framework import tool as _tool

    ran = []

    @_tool(approval_mode="always_require")
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund."""
        ran.append(order_id)
        return "refunded"

    cases_file = tmp_path / "cases.yaml"
    cases_file.write_text(
        """
cases:
  - id: approved
    input: refund A1
    approve: true
    script:
      - tool: issue_refund
        args: {order_id: A1, amount: 500}
      - reply: Refunded.
    expect: {approval_required: [issue_refund], tools: [issue_refund], contains: [refunded]}
  - id: rejected
    input: refund A2
    approve: false
    script:
      - tool: issue_refund
        args: {order_id: A2, amount: 500}
      - reply: Not refunded.
    expect: {approval_required: [issue_refund], forbidden_tools: [issue_refund]}
  - id: stops-at-pause
    input: refund A3
    script:
      - tool: issue_refund
        args: {order_id: A3, amount: 500}
    expect: {approval_required: [issue_refund]}
  - id: missing-pause
    input: hello
    script:
      - reply: hi
    expect: {approval_required: [issue_refund]}
"""
    )
    cases = {c.id: c for c in load_eval_cases(cases_file)}
    factory = lambda client: Agent(client, tools=[issue_refund])  # noqa: E731
    for case_id in ("approved", "rejected", "stops-at-pause"):
        result = await run_case(factory, cases[case_id])
        assert result.passed, result.summary()
    assert ran == ["A1"]
    missing = await run_case(factory, cases["missing-pause"])
    assert not missing.passed and "did not pause" in missing.summary()
