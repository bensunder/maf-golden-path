# Why agentkit: the boilerplate you don't write

**Short version:** a production agent on Microsoft Agent Framework (MAF) needs about fifteen things that have nothing to do with what your agent does: gateway access, identity, injection defences, PII handling, tool-loop limits, cost caps, tracing, sessions, an HTTP API, a test double for the model, an eval harness, a container, cloud infrastructure, a deploy pipeline, authenticated and resilient calls to the enterprise APIs its tools use, conversations that survive scale-out, and a human in the loop for risky actions. agentkit ships all of them, already tested against MAF 1.19. On agentkit, your service is your **tools, instructions and eval cases**, and `azd up`.

---

## 1. The numbers

These are measured from this repository, not estimated.

| | Lines of code* |
|---|---|
| agentkit packages (source): hosting, guardrails, telemetry, testing, tools | **2,521** |
| agentkit package tests | **1,273** |
| Service template, including its Bicep, azd config and deploy workflow (generated into every repo) | **695** |
| Shared platform infrastructure (Bicep + AI gateway policy) | **313** |
| CI workflows, infra validation, multi-replica smoke test, tooling | **640** |
| **Total the platform maintains once** | **~5,400** |
| What the team wrote to turn the generated project into the order-status agent | **314** |

\* Non-blank, non-comment lines.

The sample team's 314 lines break down as:
- three tools, a refund rule, and **human approval for refunds over $50** (64);
- the connector to a live carrier API, with managed-identity auth, retries and response shaping (**25**);
- instructions (11);
- eight eval cases, including approved and rejected refunds (78);
- domain tests (136).

None of it is plumbing, and it deploys with `azd up`. The carrier's OpenAPI spec (77 lines) isn't counted, because the API's owner supplies it.

### Estimated engineering time per concern

These **are estimates**. They reflect what each piece took to build and debug here, including the MAF pitfalls in section 4. Replace them with your own baseline (see section 6).

| Concern | What you'd have to build | Est. days, first time |
|---|---|---|
| Gateway-bound model client | APIM URL shapes, Entra token provider, team/quota headers, subscription keys, retries/timeouts | 1–2 |
| Config and environment policy | Typed settings, `.env`, prod guard-rails (no keys, no content capture, auth required) | 0.5–1 |
| Tool-loop limits | Iteration, call-count and duration caps wired into MAF's function-invocation config | 0.5 |
| Prompt-injection input guard | Prompt Shields client (chunking, auth, fail-closed), refusal that works for streaming and non-streaming | 2–3 |
| Indirect injection (tool output) | Scan tool results as documents before the model reads them | 1 |
| PII redaction | Patterns, Luhn check, applied before the model and to stored history | 1 |
| Tool policy | Allow/deny lists, argument validators, tell the model why instead of crashing | 1 |
| Per-session cost cap | Token accounting that survives streaming and persists with the session | 0.5–1 |
| Observability | OTel bootstrap, App Insights/OTLP, user/session/tenant on *every* span, run metrics | 2–3 |
| HTTP host | Sessions (serialize/restore), ownership checks, SSE streaming, per-session locking, probes | 2–3 |
| Model test double | A fake chat client that keeps MAF's real tool loop, middleware and telemetry | 1–2 |
| Eval harness | Case format, offline replay in CI, same cases live against the gateway | 1–2 |
| Scaffold, container, CI | Project layout, Dockerfile (non-root, prod defaults), reusable pipeline | 1–2 |
| Azure deployment | Managed identity, least-privilege role grants, Container App (probes, scale, secrets), Entra sign-in, azd wiring, OIDC deploy pipeline, post-deploy smoke and live evals | 3–6, plus waiting on the cloud team |
| API connectors | Per downstream API: auth (managed identity or on-behalf-of), retries that don't double-write, timeouts, tracing, model-friendly errors, response trimming, test fakes | 1–3 **per API** |
| Shared sessions | Session store with TTL, cross-replica locking (with crash recovery), serialization of MAF state, a scoped data-plane role | 2–4 |
| Human approvals | Pause/resume across requests and replicas, id validation, confirmation vs. separation of duties with Entra roles, audit trail, streaming, eval support | 3–5 |
| **Total per team** | | **~25–41 engineer-days, plus 1–3 per API** |

The shared AI gateway (API Management policy, per-identity token limits, chargeback metrics, Azure OpenAI behind a managed identity) is a one-time platform cost, typically another 5–10 days. agentkit ships it as `infra/platform/`.

For a new agent on agentkit, the equivalent line item is **generate the project: about a minute**. Your time then goes to the agent itself.

### Why it compounds across teams

Without a shared kit, every team pays the table above, or copies another team's version and inherits its bugs. Then every MAF upgrade has to be applied N times. MAF's Python package shipped dozens of breaking changes in 2026, some after 1.0. With agentkit the platform team absorbs each upgrade once (see [UPGRADING.md](UPGRADING.md)), and services take a version bump.

---

## 2. Before and after

### Without agentkit

A minimal *demo* agent on MAF is short:

```python
from agent_framework import Agent
from agent_framework.openai import OpenAIChatCompletionClient

client = OpenAIChatCompletionClient(model="gpt-4.1-mini", azure_endpoint=ENDPOINT, api_key=KEY)
agent = Agent(client, instructions="Help with orders.", tools=[lookup_order])
```

It is not a production service. It has:

- a key in config instead of managed identity;
- a direct model endpoint instead of the gateway where quotas and chargeback live;
- no injection defence;
- PII sent straight to the model;
- no cap on tool loops or spend;
- traces that don't say which user, session or tenant they belong to;
- no HTTP API, no sessions, no tests that run without a model.

Closing those gaps is the 15–23 days above.

### With agentkit

This is the entire agent definition in a generated service:

```python
def create_agent(settings=None, client=None):
    settings = settings or AgentKitSettings()
    return build_agent(
        name=AGENT_NAME,
        description=DESCRIPTION,
        instructions=load_instructions(INSTRUCTIONS),
        tools=TOOLS,
        tool_policy=TOOL_POLICY,
        settings=settings,
        client=client,
    )
```

And this is the HTTP service:

```python
app = create_app(create_agent)
```

And this is the deploy, to a Container App with its own managed identity, Entra sign-in, least-privilege grants and telemetry, behind the shared AI gateway:

```bash
azd up
```

Everything in the table in section 1 is attached by `build_agent`, `create_app`, `openapi_tools` and the generated `infra/`.

---

## 3. What you get, concern by concern

| You get | So you don't have to | Details |
|---|---|---|
| `build_agent()` binds the model client to the AI gateway with Entra ID, and adds `x-agentkit-team/agent/service/env` headers | Learn APIM URL shapes, token scopes and header contracts | [architecture.md](architecture.md) |
| `AgentKitSettings` reads `AGENTKIT_*` env vars and **refuses to start in prod** if config is unsafe | Write config parsing and review every PR for "is this safe in prod?" | [configuration.md](configuration.md) |
| Prompt Shields input guard, streaming-safe refusals, fail-closed | Integrate Content Safety and handle both response modes | [guardrails.md](guardrails.md) |
| Tool-output shield against indirect injection | Discover this attack class the hard way | [guardrails.md](guardrails.md) |
| PII redaction before the model and in stored history | Build and test redaction regexes | [guardrails.md](guardrails.md) |
| Tool policy: deny, allow and validate, and the model is told why | Put business rules inside prompts and hope | [guardrails.md](guardrails.md) |
| Per-session token budget | Chase one runaway conversation in the bill | [guardrails.md](guardrails.md) |
| OTel with user/session/tenant/team on every span; run metrics by outcome | Work out why your span attributes are missing on tool calls | [telemetry.md](telemetry.md) |
| FastAPI host: sessions, ownership, SSE, locking, probes | Write the same API every team writes | [architecture.md](architecture.md) |
| `ScriptedChatClient`, YAML evals (offline and live), span recorder, pytest fixtures | Mock an LLM, or let tests call a real model | [testing-and-evals.md](testing-and-evals.md) |
| Template: charter, instructions, evals, tests, Dockerfile, CI, `AGENTS.md` | Set up a new repo from scratch | [getting-started.md](getting-started.md) |
| `azd up`: service Bicep (identity, role grants, Container App, Easy Auth), preprovision checks, OIDC deploy pipeline with smoke check and **live evals as a deploy gate** | Learn Container Apps, RBAC, Easy Auth and federated credentials, then wait for the cloud team | [deploy.md](deploy.md) |
| Shared platform: AI gateway with Entra-only access, per-identity token limits, chargeback metrics, Azure OpenAI behind a managed identity | Build an APIM GenAI policy set | [deploy.md](deploy.md) |
| `openapi_tools()`: typed tools from a spec, read-only by default | Hand-write a tool per endpoint | [connectors.md](connectors.md) |
| `ManagedIdentityAuth` / `OnBehalfOfAuth` (secretless, via federated credential) | Implement OAuth on-behalf-of and store client secrets | [connectors.md](connectors.md) |
| `ApiClient`: safe retries, `Retry-After`, timeouts, trace propagation, one-line errors; `Shaper` for field selection and budgets; `mock_api` for tests | Rebuild HTTP plumbing in every tool | [connectors.md](connectors.md) |
| `gateway_mcp_tool()`: MCP with per-request tokens and an allow-list | Find out your MCP token expired after an hour | [connectors.md](connectors.md#mcp-servers) |
| Cosmos DB (managed identity) or Redis sessions, locked per conversation; 5 replicas out of the box | Design session storage and distributed locking | [sessions-and-approvals.md](sessions-and-approvals.md) |
| `approval_mode="always_require"` + `approve_if` rules; approvals API with confirmation or separation-of-duties modes; audit log; `approve:` in eval cases | Build pause/resume, authorization and audit for risky actions | [sessions-and-approvals.md](sessions-and-approvals.md#human-approvals) |

---

## 4. The pitfalls agentkit already hit for you

Each of these came up while building and testing agentkit against MAF 1.19. Each would cost a team hours of debugging.

1. **Agent middleware runs outside the `invoke_agent` span.** Set user or session attributes on the current span from agent middleware and they never reach the chat or tool spans. agentkit uses a span processor instead, so every span gets them.
2. **Replacing `context.result` in streaming mode doesn't attach your hooks.** A metric recorded that way silently never fires for streamed runs. The supported path is `context.stream_result_hooks`. agentkit's metrics and token budget use it, and tests prove it.
3. **`MiddlewareTermination` gives the caller a `None` result.** A refusal has to set `context.result` to a real response (and a `ResponseStream` when streaming). `agentkit.guardrails.refuse()` does both.
4. **Instructions aren't a system message at the client boundary.** MAF passes them as `options["instructions"]`, and the provider client adds the system message later. Tests that assert `messages[0].role == "system"` fail. `ScriptedChatClient` exposes `.instructions`.
5. **A custom chat client must follow MAF's layer contract.** `_inner_get_response` is synchronous and returns a coroutine or a `ResponseStream`. The layers need keyword-only constructor arguments. Streaming must go through `_build_response_stream`. Get any of these wrong and you get `'coroutine' object has no attribute ...` errors deep in the stack.
6. **Invalid tool arguments don't raise.** The model gets `"Error: Argument parsing failed."` and carries on. That's useful, but only if your tests expect it. The sample shows how to assert it.
7. **Chat middleware can mutate the messages sent to the model, and those objects are also what's stored in session history.** agentkit uses this on purpose: PII is redacted before the model sees it and stays redacted in history.
8. **Per-session locks in a naïve web host leak memory.** agentkit's host releases each lock when the last request for that session finishes. There's a test for it.
9. **MAF's MCP tool takes static headers.** An Entra token put there expires, usually within about an hour, and every MCP call then fails. `gateway_mcp_tool` supplies an HTTP client that fetches a token per request.
10. **The on-behalf-of credential calls `client_assertion_func` synchronously.** Bridging it to an async managed-identity credential inside the running event loop deadlocks. agentkit uses the synchronous credential, which caches its token.
11. **Keying API Management token limits on a header lets callers spoof it.** One team can drain another's quota by sending their team name. agentkit's policy keys limits on the validated token's object id, and uses headers only for reporting dimensions.
12. **The per-dimension token metrics need an App Insights setting missing from Bicep's types** (`CustomMetricsOptedInType`). Without it the gateway's chargeback dimensions silently don't appear. The platform Bicep sets it with the warning suppressed on that one line.
13. **Bicep's `@secure()` can't decorate array parameters**, so the obvious way to pass a Container App's secrets fails to compile. The template passes a secure string and builds the secret inside the module.
14. **azd's JSON schema accepts any `host:` value.** A typo only fails at deploy time. agentkit's infra check adds explicit assertions.
15. **MAF's `ToolApprovalMiddleware` raises without a session.** Once you add auto-approval rules, every plain `agent.run("...")` (workers, tests) crashes. `build_agent` puts a middleware in front that supplies a throwaway session.
16. **With auto-approval rules, MAF surfaces parallel approvals one at a time; without them, all at once.** A host that assumes either shape breaks on the other. agentkit's approval endpoint handles both, and a decision can return `approval_required` again.
17. **MAF 1.19 logs "did not match the active approval occurrence identity" on every correct resume.** It's internal double-binding, confirmed with the real OpenAI client, and the call still runs once. Suppressing it blindly would also hide real mismatches, so agentkit validates approval ids against the session's pending list *first* and only then demotes the message.
18. **Loading a session before taking its lock loses updates.** Two concurrent messages both read the old state, and the second write wins. agentkit v0.2's own host had this; v0.3 locks, then loads, runs and saves. It matters most once there are several replicas.
19. **Saving an already-expired session to Redis with a minimum TTL makes it readable for a second.** The Redis store now checks expiry on read and deletes instead of writing.
20. **Azure's async SDKs need `aiohttp`, but don't install it.** `azure.identity.aio` (managed identity) and `azure.cosmos.aio` import it only when making a request, so tests with injected tokens pass, and the service then fails its first real token request in Azure with `ImportError`. agentkit declares it, and a test checks the transport imports.

---

## 5. What agentkit does *not* do (yet)

Being honest about scope saves you time too.

- **The infrastructure hasn't been deployed to a real subscription by this repo's CI.** Every Bicep file compiles and lints clean, and names are contract-checked across platform and service (see [deploy.md](deploy.md#whats-validated-without-azure)). Run `what-if` in your subscription before first use.
- **No channel adapters yet.** Teams and M365 Copilot publishing, and an AG-UI web chat component, are on the roadmap. Today callers use the HTTP API.
- **Scored evaluators (groundedness, LLM-as-judge) aren't in the eval gate yet.** Live evals use the deterministic expectations in `cases.yaml`.
- **No HTTP endpoint for human approval** of `approval_mode="always_require"` tools. Use `TOOL_POLICY` validators for now.
- **Python only.** A .NET track is planned.

---

## 6. Measure it in your organization

The estimates in section 1 are a starting point. To replace them with your own data:

1. **Baseline:** ask the last two or three teams that shipped an agent how many engineer-days went to the concerns in section 1, and how many lines of their repo are not tools, prompts or tests.
2. **After:** for each agentkit service, record:
   - time from `copier copy` to first merged PR, and to first production deploy;
   - lines changed outside `tools.py`, `instructions/`, `evals/` and `tests/` (target: close to zero);
   - time to take a MAF upgrade (a version bump vs. a porting project).
3. **Report:** show the difference per service and the upgrade cost you avoid across N services.

The best-documented public example of this effect comes from platform engineering generally, not agents: Spotify reported that after adopting its golden-path portal (Backstage), a new engineer's time to their 10th pull request fell from about 60 days to under 20.
