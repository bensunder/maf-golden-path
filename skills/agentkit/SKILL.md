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
- Calling an existing API: generate tools with `agentkit.tools.openapi_tools(spec, client=ApiClient(url, auth=...), operations=[...])`. Never hand-write httpx/requests calls, retries or token handling. Auth: `ManagedIdentityAuth(scope)` for app-level access, `OnBehalfOfAuth(scope, client_id=..., tenant_id=...)` when the user's permissions must apply. Writes need `allow_writes=[...]` plus a `TOOL_POLICY` validator. Trim responses with `Shaper(fields=[...])`.
- High-impact actions (money, messages to customers, deletions): `@tool(approval_mode="always_require")`, plus `APPROVAL_RULES = [approve_if("tool", lambda args: ...)]` in `tools.py` for the low-risk cases. Keep hard limits in `TOOL_POLICY` validators; they still apply after approval. Add eval cases with `approve: true/false` and `expect.approval_required`.
- MCP servers: `agentkit.tools.gateway_mcp_tool(name, url, auth=..., allowed_tools=[...])`, and always set `allowed_tools`.
- Deploy: `azd up` using the generated `infra/`. Never create Azure resources by hand for a service.
- Do not add custom prompt-injection, PII or logging code in services. If a guardrail is missing, change `agentkit-guardrails` instead.

## Changing behaviour
1. Edit `src/<pkg>/instructions/system.md` (sections: Role, Objectives, Tool use, Boundaries, Style).
2. Add or adjust a case in `evals/cases.yaml` with `input`, an offline `script`, and `expect` (`contains`, `not_contains`, `tools`, `forbidden_tools`, `blocked`).
3. Run `pytest`. Offline evals must pass. Live check: `AGENTKIT_LIVE_EVALS=1 pytest tests/test_evals.py`.
4. For answer quality, add `rubric:` (one-sentence definition of a good answer) and `grounded: true` to the case, `tool_args:` to pin tool arguments, and `critical: true` for safety cases. The deploy gate (`agentkit-gate`) scores these live against `evals/baseline.json`; refresh the baseline with `--update-baseline` in the same PR when a change is intentional.

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
- Fake downstream APIs: `with mock_api(CLIENT, {"GET /orders/A1": {...}}) as calls:` (from `agentkit.tools.testing`). Use an autouse fixture in `conftest.py` so offline evals never hit the network.
- Refusals set `result.additional_properties["agentkit.blocked"]`.

## Don'ts
- No real model calls in unit tests. No network in tests.
- Don't set `AGENTKIT_CAPTURE_MESSAGE_CONTENT=true` outside local.
- Don't edit `.copier-answers.yml`. Use `copier update` to take template changes.
