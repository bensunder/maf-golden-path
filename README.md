# maf-golden-path

A paved road for building agents on **Microsoft Agent Framework (MAF) 1.19, Python**. Teams generate a service from the template and write only tools, instructions and eval cases. Model access, Entra auth, guardrails, telemetry, sessions, run limits, the HTTP API and CI come from four versioned packages owned by the platform team.

```
copier copy gh:bensunder/maf-golden-path my-agent     # new service in ~1 minute
cd my-agent && pip install -e ".[dev]" && pytest       # green offline, no model needed
```

**New here?** Read [docs/why-agentkit.md](docs/why-agentkit.md) (what it saves you), then [docs/getting-started.md](docs/getting-started.md) (your first agent in 15 minutes). Full docs: [docs/](docs/README.md).

## What's in the box

| Path | What it is |
|---|---|
| `packages/agentkit-hosting` | `build_agent()`, gateway-bound client (APIM, Azure or OpenAI-v1 style), Entra credential per environment, `AgentKitSettings` with **prod policy enforcement**, **session stores (in-memory, Cosmos DB, Redis) with cross-replica locking**, **human approvals** (`approve_if`, approvals API, confirmation or separation of duties, audit), FastAPI host (JSON + SSE, session ownership, probes) |
| `packages/agentkit-guardrails` | Prompt Shields input guard (Heuristic fallback), **tool-output injection shield**, PII redaction before the model, tool allow/deny/validators, per-session token budget |
| `packages/agentkit-telemetry` | One-call OTel bootstrap (OTLP / App Insights), span processor that stamps user (pseudonymized), session, tenant and team on **every** MAF span, run metrics by outcome |
| `packages/agentkit-tools` | `openapi_tools()` (OpenAPI → typed MAF tools, read-only by default), `ManagedIdentityAuth` / secretless `OnBehalfOfAuth`, `ApiClient` (safe retries, `Retry-After`, tracing, model-friendly errors), `Shaper`, `gateway_mcp_tool()`, `mock_api` for tests |
| `packages/agentkit-testing` | `ScriptedChatClient` (real MAF layer stack, scripted model), span recorder, YAML eval cases that run offline in CI and live against the gateway, **LLM judge + `agentkit-gate` quality gate** (repetitions, baseline, run-page report), pytest plugin |
| `template/` + `copier.yml` | Service scaffold: agent, tools, instructions, charter, evals, tests, Dockerfile, CI and deploy callers, **`azure.yaml` + `infra/` (Bicep) for `azd up`**, `AGENTS.md`/`CLAUDE.md` |
| `infra/platform/` | Shared platform, deployed once per environment: API Management AI gateway (Entra-only, per-identity token limits, chargeback metrics), Azure OpenAI behind a managed identity, Content Safety, Container Apps environment, registry, App Insights |
| `examples/order-status-agent` | A generated service after a team customized it (see its git history for the diff a team writes) |
| `.github/workflows/agent-ci.yml`, `agent-deploy.yml` | Reusable pipelines every generated service calls: test + build; OIDC `azd up` + smoke + live evals |
| `skills/agentkit/SKILL.md` | Org skill so coding assistants write code the paved-road way |
| `scripts/e2e_smoke.py` | Boots the sample and a fake gateway with uvicorn and checks the whole HTTP path |
| `scripts/check_infra.py`, `platform_env.py` | Offline infra validation (Bicep, azd schema, actionlint, platform↔service contract); platform outputs → `azd env` / GitHub variables |

## What a team writes vs. what it gets

In the sample, the team-authored code is `tools.py` (3 tools + a refund policy), a 25-line `connectors.py` (live carrier API from its OpenAPI spec), approval rules for large refunds, `instructions/system.md` and `evals/cases.yaml`. Everything below is inherited:

| Concern | How it's handled | Where |
|---|---|---|
| Model access | Only via the AI gateway; `x-agentkit-team/agent/service` headers for APIM quotas and chargeback; APIM subscription key | `hosting.clients` |
| Auth | Managed identity in prod (enforced), Azure CLI / Default locally; bearer token provider, no keys in code | `hosting.clients`, `hosting.settings` |
| Tool-loop limits | `max_iterations`, `max_function_calls`, `max_run_seconds` → MAF `FunctionInvocationConfiguration` | `hosting.clients` |
| Prompt injection (direct) | Prompt Shields (fail-closed), refused before any model call, streaming-safe | `guardrails.InputGuardMiddleware` |
| Prompt injection (indirect) | Tool results scanned as *documents*; poisoned output withheld from the model | `guardrails.ToolOutputShieldMiddleware` |
| PII | Email/SSN/card(Luhn)/phone redacted before the model and in stored history | `guardrails.PiiRedactionMiddleware` |
| Dangerous tools | Deny list, allow list, argument validators; rejected calls never execute, model is told why | `guardrails.ToolPolicyMiddleware` |
| Runaway cost | Per-session token budget persisted in session state | `guardrails.SessionTokenBudgetMiddleware` |
| Tracing | GenAI semconv spans from MAF + user/session/tenant/team on every span; content capture blocked in prod | `telemetry` |
| Sessions | Serialized `AgentSession` in a TTL/LRU store behind a `SessionStore` protocol; owner-checked | `hosting.sessions`, `hosting.app` |
| Tests | Scripted model, offline evals, HTTP contract tests, generated with the project | `testing`, `template/tests` |
| Quality gate | Live evals 3x per deploy: judged rubric and groundedness, argument and budget checks, critical cases, baseline regressions block the deploy | `testing.gate`, `agent-deploy.yml` |
| Scale-out | Sessions in Cosmos DB (managed identity), locked per conversation; 5 replicas | `hosting.sessions`, `template/infra` |
| Human in the loop | Risky tools pause for approval; confirmation or separation of duties via Entra app roles; audit log; eval support | `hosting.approvals` |
| Deploy | `azd up`: managed identity, least-privilege grants, Container App with Entra sign-in; OIDC pipeline with smoke check and live evals as a gate | `template/infra`, `agent-deploy.yml` |
| Model access at org level | AI gateway: Entra-only, per-identity token limits, chargeback metrics, models behind a managed identity | `infra/platform` |
| Calling enterprise APIs | Tools generated from OpenAPI; managed-identity or secretless on-behalf-of auth; safe retries, tracing, response shaping | `tools` |

## Prod policy (enforced at startup, not by review)

`AGENTKIT_ENVIRONMENT=prod` refuses to start unless: `auth_mode=managed_identity`, a gateway endpoint is set, `guardrail_mode=prompt_shields` with a Content Safety endpoint, `require_user=true`, and message-content capture is off.

## Develop the kit

```
python -m venv .venv && . .venv/bin/activate
make install     # editable installs of all packages + sample
make test        # packages, sample, template (both modes), end-to-end smoke
```

Status: **v0.4.0, milestone 4**: a quality gate for deploys: judged rubric and groundedness scores, tool-argument and budget checks, critical cases, repetitions and baseline comparison, with the results table on the run page. Earlier: shared sessions and human approvals (v0.3), deploy and connectors (v0.2). Not yet included: channel adapters (Teams, AG-UI web chat) and the .NET track. See `docs/UPGRADING.md` for the MAF version policy.
