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

For tools that need human approval, add `approve: true|false` to the case (decide every approval and continue), and `approval_required: [tool, ...]` under `expect` (these must have paused). `approval_required: []` asserts no pause happened. See [sessions-and-approvals.md](sessions-and-approvals.md#testing).

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

## The quality gate (deploys)

Offline evals prove the wiring. They can't tell you whether a new prompt, model version or MAF upgrade made the agent *worse*. The **quality gate** runs the same `cases.yaml` live, several times per case, scores the answers, compares them with a committed baseline, and fails the deploy on regression.

### Scored and budget checks

| `expect:` key | Checked by | Meaning |
|---|---|---|
| `tool_args: {tool: {arg: value}}` | deterministic | Some call to `tool` had at least these arguments (strings compared case-insensitively) |
| `max_tool_calls: n` | deterministic | No more than `n` tool executions |
| `max_total_tokens: n` | deterministic | Total model tokens for the case (all rounds, including after approvals) |
| `rubric: "…"` (+ `min_score`, default 4) | LLM judge, 1–5 | Your definition of a good answer, in one or two sentences |
| `grounded: true` (+ `grounded_min_score`, default 4) | LLM judge, 1–5 | Every factual claim in the reply is supported by what the tools returned. The judge sees the tool calls and results |

Case-level keys: `critical: true` means the case must pass on **every** repetition (use it for safety cases: injection blocked, rejected refunds never issued). `repeat: n` overrides the repetition count for one case.

A misspelled `expect` key is an error when the file loads, so a typo can't silently check nothing.

The judge runs through the same gateway and managed identity as the agent, at temperature 0. It is told not to follow instructions inside the material it grades. Anything other than a clean `{"score": 1-5}` counts as a **failure**, never a pass. Offline (plain `pytest`), judged checks are reported as skipped.

### Gate rules

`agentkit-gate` exits 1 (and fails the deploy) when any of these hold:

1. a `critical` case failed on any repetition;
2. the mean pass rate across cases is below `--min-pass-rate` (default 0.9);
3. a case's pass rate fell more than `--max-regression` (default 0.15) below its baseline;
4. a case's mean judge score fell more than `--score-tolerance` (default 0.5) below its baseline.

### Running it

```bash
# what the deploy pipeline runs (after azd up), with gateway settings from the environment:
agentkit-gate --cases evals/cases.yaml --factory my_agent:create_agent --live --repeat 3 \
  --baseline evals/baseline.json --report eval-report.json

# set or refresh the baseline from a good build: review the diff, then commit it
agentkit-gate --cases evals/cases.yaml --factory my_agent:create_agent --live --repeat 5 \
  --baseline evals/baseline.json --update-baseline
```

The results table goes to the GitHub run page (`$GITHUB_STEP_SUMMARY`), and the full JSON report is uploaded as the `eval-report-<env>` artifact:

```
## ❌ Agent quality gate: FAILED
Mode: live · repetitions: 3 · mean pass rate: 83% · baseline: yes · 41s
**Why it failed:**
- `shipped-order-status` rubric score regressed 4.7 → 3.3
| Case | Pass rate | Scores | Notes |
| `shipped-order-status` | 3/3 | rubric 3.3, grounded 5.0 | rubric scored 3 < 4: promised a delivery time… |
| `prompt-injection-blocked` 🔒 | 3/3 | – | |
```

Use `AGENTKIT_JUDGE_MODEL` to judge with a different deployment than the agent's, often a stronger one. If that model rejects a temperature setting, pass `--judge-temperature none` (the `judge-temperature` input of the deploy workflow).

### Baseline workflow

- **No baseline yet:** the gate applies rules 1–2 only. Create one from a build you're happy with and commit it.
- **Intentional behaviour change** (a new policy, say): update the cases, run with `--update-baseline`, and commit the new `baseline.json` in the same PR, so the reviewer sees the quality change next to the code change.
- **Model or MAF upgrade:** run the gate against the new version with the old baseline. A regression is exactly what you want to find out before production.

## The HTTP contract

`tests/test_app.py` drives the real FastAPI app with `TestClient` and a scripted model. Use it for anything about sessions, identity headers or response shape. It also runs a turn through the AG-UI endpoint when web chat is on.

## Judge calibration and live runs

- `agentkit-gate --calibrate evals/judge_calibration.yaml` scores the judge against answers a person graded: agreement, kappa, false passes. See [operations.md](operations.md#judge-calibration).
- `AGENTKIT_EVAL_USERS` (JSON) maps the test users named in cases (`user:`) to real accounts for live runs; `--skip-user-cases` skips those cases where there are no test accounts.
- `--skip <case id>` skips a case the environment can't serve (e.g. a downstream API that isn't there). A typo in the id is an error, not a silent no-op.

## Knowledge

Eval cases can run as a user (`user:`) and check citations (`cites:`) and permissions (`must_not_retrieve:`, which checks what the *search returned*, not what the answer says). The generated `conftest.py` searches your real `knowledge/` folder offline through the real ingestion pipeline. See [knowledge.md](knowledge.md#testing).

## Channels

`agentkit.channels.testing.TeamsTestClient` sends real Bot Framework activities into your app and records what the agent posts back, through a fake Bot Connector, with no Teams and no network. You can click approval cards as different users, which is how you test separation of duties. See [channels.md](channels.md#testing-channels).

## A gate report from the offline run

The offline evals run inside `pytest`, with your fixtures (fake APIs, the fake search index, test users), which the `agentkit-gate` CLI doesn't have. To get a gate report from that run, in the same JSON format:

```bash
pytest --agentkit-eval-report build/gate-report.json
```

Every scripted case must pass (the wiring works or it doesn't). `agent-deploy` writes this report into the image, and the [console](console.md) shows it on the Evaluations page, with each case's duration.

## What CI runs

The reusable `agent-ci.yml` pipeline runs `pytest` (unit, offline evals, HTTP), publishes JUnit results and builds the container. Everything is deterministic, and no model or secrets are needed. Live, scored evals run in the **deploy** pipeline through the quality gate above.
