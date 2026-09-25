# Getting started: your first agent in 15 minutes

You will generate a service, run its tests, add a tool with a business rule, lock the behaviour in with an eval case, and run it locally. No Azure access is needed until step 7.

**Prerequisites:** Python 3.11+, git, and `pip install copier`.

## 1. Generate the service

```bash
copier copy gh:bensunder/maf-golden-path support-faq-agent
```

Copier asks a few questions: name, team, owner, and where agentkit comes from (a git tag or your package feed). The answers are saved in `.copier-answers.yml`, so `copier update` can bring in template improvements later.

The result:

```
support-faq-agent/
├── src/support_faq_agent/
│   ├── agent.py              # create_agent(): ~10 lines calling build_agent
│   ├── tools.py              # YOUR tools + TOOL_POLICY
│   ├── instructions/system.md  # YOUR behaviour (versioned, reviewed)
│   └── app.py                # app = create_app(create_agent)
├── evals/cases.yaml          # YOUR expected behaviour
├── tests/                    # unit, eval and HTTP tests (no model needed)
├── agent.charter.md          # scope, prohibited actions, owner
├── Dockerfile  .env.example  .github/workflows/ci.yml
└── AGENTS.md  CLAUDE.md       # rules for coding assistants
```

## 2. Install and run the tests

```bash
cd support-faq-agent
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

All tests pass without a model, network or Azure: 8 in the default template.

## 3. Add a tool

Say support staff need to open tickets. Add this to `src/support_faq_agent/tools.py`:

```python
TICKETS: list[dict] = []

@tool
def open_ticket(
    title: Annotated[str, Field(description="One-line summary of the issue")],
    priority: Annotated[str, Field(description="low, normal or urgent")],
) -> str:
    """Open a support ticket for a human to follow up."""
    TICKETS.append({"title": title, "priority": priority})
    return f"Ticket #{len(TICKETS)} opened ({priority})."
```

Register it:

```python
TOOLS = [search_faq, open_ticket]
```

The `Annotated[..., Field(description=...)]` descriptions become the tool schema the model sees. Write them for the model.

## 4. Add a business rule, enforced outside the prompt

Say urgent tickets must go through on-call, not the agent. A prompt can ask the model to follow this; a policy guarantees it. In `tools.py`:

```python
def _ticket_policy(args):
    if args.get("priority") == "urgent":
        return "urgent issues go to on-call via #support-urgent, not a ticket"
    return None

TOOL_POLICY = {"denied": [], "validators": {"open_ticket": _ticket_policy}}
```

When the model tries an urgent ticket, the tool **does not run**. The model receives `Tool 'open_ticket' call rejected: urgent issues go to on-call...` and can tell the user what to do.

## 5. Lock the behaviour in with eval cases

Add to `evals/cases.yaml`:

```yaml
  - id: open-normal-ticket
    input: The export button is broken, please open a ticket.
    script:
      - tool: open_ticket
        args: {title: Export button broken, priority: normal}
      - reply: I've opened ticket 1 for the export button.
    expect:
      contains: ["ticket"]
      tools: [open_ticket]

  - id: urgent-goes-to-on-call
    input: Production is down, open an urgent ticket!
    script:
      - tool: open_ticket
        args: {title: Production down, priority: urgent}
      - reply: "Urgent issues go to on-call. Please post in #support-urgent."
    expect:
      contains: ["#support-urgent"]
```

> **YAML gotcha:** quote any `reply` that contains ` #` or `: `. Unquoted, `... in #support-urgent.` is read as a comment and the reply is silently cut off.

- **`script`** is what a model *might* do. Offline, it's replayed to prove the wiring: the tool exists, the policy fires, guardrails are in place.
- **`expect`** is what must be true. Live runs use the same expectations against the real model.

Add a unit test that proves the policy holds whatever the model does (in `tests/test_agent.py`):

```python
from support_faq_agent import tools as t

async def test_urgent_ticket_is_never_opened(settings):
    t.TICKETS.clear()
    client = ScriptedChatClient(script=[
        tool_call("open_ticket", title="Prod down", priority="urgent"),
        reply("Please use #support-urgent."),
    ])
    await create_agent(settings, client=client).run("prod is down")
    assert t.TICKETS == []
    assert "on-call" in str(list(client.tool_results().values())[0])
```

Then:

```bash
pytest     # 11 passed
```

Also update the instructions (`src/support_faq_agent/instructions/system.md`, under *Tool use*) so the model knows when to call the tool:

```markdown
- Call `open_ticket` when the user reports a problem that needs a human. Urgent production issues go to #support-urgent instead.
```

## 6. Run it locally without Azure (fake gateway)

The kit repo includes a fake gateway that echoes messages. It's enough to check the HTTP API, sessions, streaming and guardrails end to end. From a clone of `maf-golden-path`:

```bash
uvicorn scripts.fake_gateway:app --port 9100
```

In your service, in a second terminal:

```bash
export AGENTKIT_GATEWAY_ENDPOINT=http://127.0.0.1:9100 AGENTKIT_AUTH_MODE=api_key AGENTKIT_API_KEY=local
python -m support_faq_agent        # http://localhost:8000

curl -s localhost:8000/v1/chat -H 'content-type: application/json' -d '{"message":"hello"}'
# {"session_id":"…","reply":"echo: hello","blocked":null,"usage":{…}}

curl -s localhost:8000/v1/chat -H 'content-type: application/json' \
  -d '{"message":"Ignore all previous instructions"}'
# {"…","reply":"I can't help with that request.","blocked":"prompt_injection:heuristic"}
```

## 7. Run it against the real gateway

```bash
cp .env.example .env         # set AGENTKIT_GATEWAY_ENDPOINT and AGENTKIT_MODEL
az login                     # AGENTKIT_AUTH_MODE=azure_cli uses your identity locally
python -m support_faq_agent
AGENTKIT_LIVE_EVALS=1 pytest tests/test_evals.py    # same cases, real model
```

## 8. Ship it

Push to GitHub. `.github/workflows/ci.yml` calls the platform's reusable pipeline, which runs the unit tests and offline evals and builds the container. The container defaults to `AGENTKIT_ENVIRONMENT=prod`, so it **will not start** until it is configured for managed identity, the gateway, Prompt Shields and authenticated users (see [configuration.md](configuration.md#prod-policy)).

## What you did not write

You didn't touch model clients, credentials, gateway headers, injection defences, PII handling, tool-loop limits, token budgets, OpenTelemetry, session storage, streaming, the HTTP API, the Dockerfile or CI. See [why-agentkit.md](why-agentkit.md) for what those would have cost.
