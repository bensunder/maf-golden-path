---
name: agentkit
description: Use when creating or changing a Microsoft Agent Framework (MAF) agent service in this organization, or when a repo contains .copier-answers.yml from maf-golden-path. Covers scaffolding, tools, instructions, evals, tests and config the paved-road way.
---

# Building agents on the agentkit paved road

## New service
Run `copier copy gh:bensunder/maf-golden-path <dest>`, then `pip install -e ".[dev]" && pytest`. Do not hand-write a new MAF service.

## Hard rules
- Build agents only with `agentkit.hosting.build_agent(...)` (see `src/<pkg>/agent.py`). Never instantiate `agent_framework.Agent`, `OpenAIChatClient`, `AzureOpenAI` or credentials directly. `build_agent` supplies the gateway client, Entra auth, guardrails, telemetry and run limits.
- Configuration comes from `AGENTKIT_*` env vars via `AgentKitSettings`. Never hard-code endpoints, keys, model names or limits.
- Tools are `@tool` functions in `tools.py`, registered in `TOOLS`. Use `Annotated[type, Field(description=..., pattern/gt/...)]` for every parameter.
- State-changing tools (refunds, emails, tickets, writes) need a validator in `TOOL_POLICY["validators"]`, and/or `@tool(approval_mode="always_require")` when a human-approval UI exists.
- Do not add custom prompt-injection, PII or logging code in services. If a guardrail is missing, change `agentkit-guardrails` instead.

## Changing behaviour
1. Edit `src/<pkg>/instructions/system.md` (sections: Role, Objectives, Tool use, Boundaries, Style).
2. Add or adjust a case in `evals/cases.yaml` with `input`, an offline `script`, and `expect` (`contains`, `not_contains`, `tools`, `forbidden_tools`, `blocked`).
3. Run `pytest`. Offline evals must pass. Live check: `AGENTKIT_LIVE_EVALS=1 pytest tests/test_evals.py`.

## Testing pattern
```python
from agentkit.testing import ScriptedChatClient, reply, tool_call
client = ScriptedChatClient(script=[tool_call("lookup_order", order_id="A1001"), reply("Shipped.")])
agent = create_agent(settings, client=client)          # `settings` fixture from conftest.py
result = await agent.run("Where is A1001?")
assert client.tool_calls_made()[0].name == "lookup_order"
assert "Shipped" in result.text
```
- `client.calls[i].instructions`, `.last_user_text`, `client.tool_results()`, `client.assert_script_consumed()`.
- Unit-test a tool directly with `my_tool.func(...)`.
- Refusals set `result.additional_properties["agentkit.blocked"]`.

## Don'ts
- No real model calls in unit tests. No network in tests.
- Don't set `AGENTKIT_CAPTURE_MESSAGE_CONTENT=true` outside local.
- Don't edit `.copier-answers.yml`. Use `copier update` to take template changes.
