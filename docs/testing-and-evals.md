# Testing and evals

## The rule: unit tests never call a model

`agentkit.testing.ScriptedChatClient` replaces the model and nothing else. It is built from MAF's real layers (function invocation, chat middleware, telemetry), so your tools run, your middleware runs, guardrails run and spans are emitted exactly as in production. Only the model's answers are scripted.

```python
from agentkit.testing import ScriptedChatClient, reply, tool_call

async def test_order_status(settings):
    client = ScriptedChatClient(script=[
        tool_call("lookup_order", order_id="A1001"),          # model turn 1: call a tool
        reply("A1001 shipped with UPS."),                     # model turn 2: answer
    ])
    agent = create_agent(settings, client=client)
    result = await agent.run("Where is A1001?")

    assert result.text == "A1001 shipped with UPS."
    assert client.tool_calls_made()[0].arguments == {"order_id": "A1001"}
    assert "1Z999AA10123456784" in str(list(client.tool_results().values())[0])  # what the model saw
    client.assert_script_consumed()
```

### Script building blocks

| Helper | Model turn |
|---|---|
| `reply("text")` | Answers with text |
| `reply("text", input_tokens=10, output_tokens=5)` | Answers and reports usage (for budget tests) |
| `tool_call("name", **args)` | Calls one tool |
| `Turn(tool_calls=(ToolCall(...), ToolCall(...)))` | Calls several tools at once |
| `lambda messages: "..."` | Computes the answer from what was sent |
| `client.enqueue(...)` | Adds turns later (fixtures, multi-request tests) |

### Inspecting what happened

| Call | Returns |
|---|---|
| `client.calls` | One `RecordedCall` per model call |
| `client.calls[i].instructions` | The system instructions sent (MAF passes them as an option, not a message) |
| `client.calls[i].last_user_text` | The user text the model received, after PII redaction |
| `client.calls[i].messages` | The full message list, including tool results |
| `client.tool_calls_made()` | Every tool call the model emitted |
| `client.tool_results()` | `call_id → result` as the model saw it, including policy rejections and withheld output |
| `client.assert_script_consumed()` | Fails if the agent made fewer model calls than scripted |

If the agent makes *more* calls than scripted, `ScriptExhaustedError` names the call and the last user text.

### Testing tools directly

A `@tool` function is still a function:

```python
def test_lookup_order():
    assert lookup_order.func("A1001")["status"] == "shipped"
```

### Asserting on spans

The `span_recorder` fixture comes with the pytest plugin, no import needed:

```python
async def test_emits_agent_span(settings, span_recorder):
    await create_agent(settings, client=ScriptedChatClient(script=[reply("ok")])).run("hi")
    assert any(name.startswith("invoke_agent") for name in span_recorder.names())
```

## Evals: behaviour as data

`evals/cases.yaml` is the contract for your agent. The same file runs in two modes:

| Mode | When | Model | Proves |
|---|---|---|---|
| Offline (default) | Every PR, in CI | `script` replayed | Tools exist, policies fire, guardrails block, expectations are consistent |
| Live (`AGENTKIT_LIVE_EVALS=1`) | Before deploy, against the dev gateway | Real model, `script` ignored | The model actually behaves as expected |

```yaml
cases:
  - id: large-refund-escalates
    input: Refund the full $129 on A1002, they changed their mind.
    script:                              # offline only
      - tool: lookup_order
        args: {order_id: A1002}
      - tool: issue_refund
        args: {order_id: A1002, amount: 129, reason: changed mind}
      - tool: escalate_to_human
        args: {order_id: A1002, summary: Over limit}
      - reply: That's over the $50 limit, so I've opened a ticket for a specialist.
    expect:
      contains: ["ticket"]               # case-insensitive substrings of the reply
      not_contains: ["refunded"]
      tools: [escalate_to_human]         # must have been called (and allowed to run)
      forbidden_tools: [delete_account]  # must not have been called
      blocked: false                     # guardrail outcome
```

Script steps: `reply: <text>`, `tool: <name>` with `args`, or `tools: [{tool, args}, ...]` for parallel calls.

**YAML gotcha:** quote any text containing ` #` or `: `, e.g. `reply: "Post in #support-urgent."`. Unquoted, everything after ` #` is a comment.

### Writing good cases

- **One case per behaviour you'd be upset to lose.** Add a case for every bug you fix.
- **Cover the edges:** unknown ids, over-limit requests, off-topic asks, injection attempts.
- **In live mode, assert on outcomes, not wording.** `contains: ["ticket"]` survives model upgrades; `contains: ["I have created ticket SUP-1234 for you"]` doesn't.
- **Use `forbidden_tools` for dangerous tools.** It's the cheapest safety regression test there is.

### Running

```bash
pytest tests/test_evals.py                          # offline
AGENTKIT_LIVE_EVALS=1 pytest tests/test_evals.py    # live, uses your AGENTKIT_* settings
```

Each case is its own test (`test_eval_case[large-refund-escalates]`), and failures list every unmet expectation:

```
[FAIL] large-refund-escalates — reply missing 'ticket'; tool 'escalate_to_human' not called (called: ['lookup_order'])
```

## The HTTP contract

`tests/test_app.py` drives the real FastAPI app with `TestClient` and a scripted model. Use it for anything about sessions, identity headers or response shape.

## What CI runs

The reusable `agent-ci.yml` pipeline runs `pytest` (unit, offline evals, HTTP), publishes JUnit results and builds the container. Everything is deterministic, and no model or secrets are needed. Live evals as a deploy gate are on the roadmap.
