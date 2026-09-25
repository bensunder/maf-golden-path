# Why agentkit: the boilerplate you don't write

**Short version:** a production agent on Microsoft Agent Framework (MAF) needs about fifteen things that have nothing to do with what your agent does: gateway access, identity, injection defences, PII handling, tool-loop limits, cost caps, tracing, sessions, an HTTP API, a test double for the model, an eval harness, a container, cloud infrastructure, a deploy pipeline, authenticated and resilient calls to the enterprise APIs its tools use, conversations that survive scale-out, a human in the loop for risky actions, a quality gate that blocks a deploy when answers get worse, a way for people to actually reach the agent (Teams and a web chat), and answers from company documents that respect who may read them. agentkit ships all of them, already tested against MAF 1.19. On agentkit, your service is your **tools, instructions and eval cases**, and `azd up`.

---

## 1. The numbers

These are measured from this repository, not estimated.

| | Lines of code* |
|---|---|
| agentkit packages (source): hosting, guardrails, telemetry, testing, tools, channels (incl. the web chat component), knowledge | **5,438** |
| agentkit package tests (incl. Teams, AG-UI, headless-browser and search wire-format tests) | **2,437** |
| Service template, including its Bicep, azd config, deploy workflow, Teams packaging and knowledge (generated into every repo) | **1,258** |
| Shared platform infrastructure (Bicep + AI gateway policy) | **336** |
| CI workflows, infra validation, multi-replica, channel and live-gate smoke test, tooling | **892** |
| **Total the platform maintains once** | **~10,400** |
| What the team wrote to turn the generated project into the order-status agent | **426** |

\* Non-blank lines, excluding comment-only lines, counted by one script across the repo (v0.5.0 and later; earlier versions of this page used a slightly different count).

The sample team's 426 lines break down as:
- three tools, a refund rule, **human approval for refunds over $50**, and the policy-document search (60);
- the connector to a live carrier API, with managed-identity auth, retries and response shaping (**19**);
- instructions (15);
- eleven eval cases, which double as the deploy **quality gate**, with judged rubrics, groundedness, citations and critical safety cases, including "a support agent never retrieves the leads-only playbook" (120);
- domain tests (207), including a Teams test that a refunds lead, and never the requester, approves a large refund from a card, and tests that each person only finds the documents they may read;
- who may read each policy document (`acl.yaml`, 5).

Reaching the agent from **Teams and a web chat** took two copier answers and no code. Answering from **policy documents, trimmed per user and cited**, took one copier answer, three Markdown files and an `acl.yaml`. None of the 426 lines is plumbing, and it deploys with `azd up`. The carrier's OpenAPI spec (77 lines) isn't counted, because the API's owner supplies it.

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
| Quality gate | LLM judge (rubric, groundedness) that fails closed, argument and budget checks, repetitions, baseline comparison, run-page report, deploy wiring | 3–5 |
| **Total per team** | | **~28–46 engineer-days, plus 1–3 per API** |
| *If users reach it through Teams* | Bot endpoint with JWT validation, secretless bot registration, background turns and proactive replies, per-user sessions, app manifest; approval cards with approver checks, click-once updates, an approvals channel; offline test harness | 5–8 |
| *If users reach it through a web page* | Streaming protocol with approvals and resume, server-held history, thread ownership, a chat UI that can't be XSS'd, CSP, browser sign-in, CSRF | 3–6 |
| *If it answers from documents* | Search trimmed to each user's (nested) Entra groups that fails closed, hybrid + semantic query, citations in every channel, ingestion with access rules, extraction, chunking, embeddings and incremental sync, search/Document Intelligence/storage with least-privilege RBAC, permission-aware evals | 6–10 |

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

Closing those gaps is most of the table above.

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

And this is the HTTP service, reachable from Teams and a web chat as well as the JSON API:

```python
app = create_app(create_agent, channels=[AgUiChannel(), WebChat(), *teams_from_env()])
```

And this is the deploy, to a Container App with its own managed identity, Entra sign-in, least-privilege grants and telemetry, behind the shared AI gateway:

```bash
azd up
```

Everything in the table in section 1 is attached by `build_agent`, `create_app`, the channels, `openapi_tools` and the generated `infra/`.

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
| Quality gate: `rubric`, `grounded`, `tool_args`, budgets and `critical` cases, run live 3x per deploy against a committed baseline; results on the run page | Build an LLM-judge harness, then argue about whether the new prompt is better | [testing-and-evals.md](testing-and-evals.md#the-quality-gate-deploys) |
| `approval_mode="always_require"` + `approve_if` rules; approvals API with confirmation or separation-of-duties modes; audit log; `approve:` in eval cases | Build pause/resume, authorization and audit for risky actions | [sessions-and-approvals.md](sessions-and-approvals.md#human-approvals) |
| Teams: bot endpoint, secretless Azure Bot, approvals as Adaptive Cards in an approvers channel, Entra-group approvers, app package script, offline `TeamsTestClient` | Learn the M365 Agents SDK, Bot Framework auth and Teams' timeouts | [channels.md](channels.md#microsoft-teams) |
| Web chat at `/chat` and AG-UI at `/v1/agui`, with approvals as standard interrupts | Build and secure a chat UI and its streaming protocol | [channels.md](channels.md#web-chat) |
| `knowledge_tool()`: company documents searched **as the signed-in user**, with citations in every channel; `agentkit-ingest` with `acl.yaml`; `cites:` / `must_not_retrieve:` eval checks | Build RAG, then find out it shows everyone everything | [knowledge.md](knowledge.md) |

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
21. **An LLM judge that defaults to "pass" when it can't parse its own output hides regressions.** Judges return prose, markdown fences or out-of-range numbers. agentkit's judge accepts only a clean 1–5 score. Anything else, including the judge call failing, counts as a failure.
22. **A misspelled expectation checks nothing.** `contain:` instead of `contains:` silently passes forever. The case loader rejects unknown `expect` keys.
23. **Evals running inside a prod deploy inherit prod policy.** The deploy job's `AGENTKIT_ENVIRONMENT=prod` made the eval step reject its own CI credentials, so live evals could never run in a production deploy. This affected agentkit's own v0.2–v0.3 pipeline. The eval step now runs as `test`; the prod policy applies to the deployed service.
24. **Teams gives a bot about 15 seconds to answer, and agent runs with tools take longer.** The SDK's `long_running_messages` option still holds the request open until the run ends. agentkit acknowledges with 202 at once, runs the turn in the background and replies proactively.
25. **Proactive replies are sent as replies to a message that doesn't exist.** The SDK gives each continuation a random activity id, and `send_activity` copies it into `replyToId`. agentkit clears it, so approval cards start a new post in the approvals channel. The fake Bot Connector in the tests caught this.
26. **The SDK's `serviceUrl` allow-list is off by default.** A forged activity could make the bot send token-bearing calls to any host. agentkit turns it on (Microsoft hosts only, plus explicit test hosts), and a test proves a foreign host is refused before any call.
27. **`AgentApplication` requires a Storage and writes conversation and user state on every turn.** With `MemoryStorage` that grows per user, per replica, and isn't shared. agentkit gives the SDK a no-op storage and keeps state in its own session store.
28. **Easy Auth in front of the bot endpoint rejects every Teams message**, because the Bot Connector's token isn't for your app. The SDK's JWT middleware applied app-wide rejects every normal API call instead. agentkit validates Bot Framework tokens on `/api/messages` only, and the Bicep excludes that one path from Easy Auth.
29. **Adaptive Cards render Markdown, and tool arguments come from the model.** An injected `[click here](https://…)` becomes a link in front of the approver. agentkit escapes every value on approval cards.
30. **AG-UI clients send the whole conversation on every run.** Trusting it lets a browser rewrite what the agent said. MAF's own AG-UI endpoint also keeps approval state in process memory and doesn't tie a thread to a user. agentkit keeps history and approvals in the shared session store, owner-checked, and uses only the newest user message.
31. **A cookie-authenticated chat page opens the API to CSRF** on FastAPI versions that parse `text/plain` bodies as JSON. agentkit rejects non-JSON POSTs app-wide (415), so a cross-site form can't act as the signed-in user.
32. **`from __future__ import annotations` plus a locally imported FastAPI `Request`** makes FastAPI read the parameter as a required query field, and every call returns 422. It happened twice while building the channels. The endpoints now import at module level or register plain Starlette routes.
33. **MAF's Azure AI Search context provider has no per-user filter.** In semantic mode it searches as the app, so every user retrieves every document the app can read; agentic mode takes one fixed credential. It also drops document ids and titles, so there's nothing to cite. agentkit's knowledge tool filters by the caller's groups and returns numbered, citable sources.
34. **The groups claim in a user's token isn't enough.** It's omitted for users in many groups (overage), it doesn't include nested groups, and Teams turns carry no user token at all. agentkit asks Microsoft Graph for transitive membership, caches it, and fails closed on errors.
35. **Filtering vector results after the nearest-neighbour search hides relevant documents** from users with narrow access: the top matches are picked from the whole index, then trimmed, sometimes to nothing. agentkit sets `vectorFilterMode: preFilter`, and a wire-level test checks that it's sent.
36. **`fnmatch`'s `*` matches `/`.** An access rule for `*.md` meant for top-level documents also matched `refund-leads/playbook.md`, and once the leads rule was removed, the leads-only playbook became readable by everyone. The kit's own test caught it. agentkit's `acl.yaml` globs are path-aware, like `.gitignore`.
37. **Keeping a document's old chunks when its rule is removed or its extraction fails** leaves it indexed with its old access groups. agentkit's ingestion removes anything it didn't successfully process; the next good run adds it back.
38. **Passing `params` to httpx replaces the query string of a Graph `@odata.nextLink`.** The `$skiptoken` disappears and paging fetches page one forever. agentkit carries the link's query over as params, and a test checks the second request.
39. **Numbering sources per tool call makes `[1]` mean two documents** when the agent searches twice in one answer. agentkit numbers per run, so citations stay unambiguous.
40. **An eval that only checks the answer can't catch a permission leak:** the model may politely ignore a document it shouldn't have seen. `must_not_retrieve:` checks what the search returned.

---

## 5. What agentkit does *not* do (yet)

Being honest about scope saves you time too.

- **The infrastructure hasn't been deployed to a real subscription by this repo's CI.** Every Bicep file compiles and lints clean, and names are contract-checked across platform and service (see [deploy.md](deploy.md#whats-validated-without-azure)). Run `what-if` in your subscription before first use.
- **Knowledge hasn't run against a real Azure AI Search service yet.** The query is tested against the real SDK over HTTP, and ingestion against a fake index. See [knowledge.md](knowledge.md#not-included-yet).
- **The Teams channel hasn't run in a real Teams tenant yet.** It's tested offline with real Bot Framework activities through the M365 Agents SDK, a fake Bot Connector, and a headless browser for the web chat. See [channels.md](channels.md#not-included-yet) for what that leaves open, including Teams SSO for on-behalf-of tools and Microsoft 365 Copilot publishing.
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
