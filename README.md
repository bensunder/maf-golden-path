# 🧩 maf-golden-path 
Save an estimated 28–46 engineer-days per enterprise MAF agent.Generate the project in ~1 minute. Write the business logic. Inherit security, approvals, sessions, telemetry, testing, evaluation, knowledge, channels, infrastructure, and CI/CD. 
A paved road for building agents on **Microsoft Agent Framework (MAF) 1.19, Python**. Teams generate a service from the template and write only tools, instructions and eval cases. Model access, Entra auth, guardrails, telemetry, sessions, approvals, run limits, the HTTP API, **Microsoft Teams and a web chat**, **answers from company documents trimmed to what each user may read**, and CI come from versioned packages owned by the platform team.

# The paved-road workflow:

<img width="1136" height="943" alt="image" src="https://github.com/user-attachments/assets/6f1783fd-05d2-4e15-9d8b-94c7a3903adf" />

# Architecture

<img width="1268" height="832" alt="image" src="https://github.com/user-attachments/assets/839b979f-b776-4b0b-a590-834c604c0592" />


     
# 🏗️ What you actually build

A generated service keeps the application team's surface area intentionally small.

Your team owns

tools.py · connectors.py · instructions · approval rules · evaluation cases · business tests

The platform owns

authentication · model access · security · sessions · approvals · telemetry · evaluation infrastructure · knowledge access · channels · Azure deployment · CI/CD

# 🧩 What's in the box
| Path | What it is |
|---|---|
| `packages/agentkit-hosting` | `build_agent()`, gateway-bound client (APIM, Azure or OpenAI-v1 style), Entra credential per environment, `AgentKitSettings` with **prod policy enforcement**, **session stores (in-memory, Cosmos DB, Redis) with cross-replica locking**, **human approvals** (`approve_if`, approvals API, confirmation or separation of duties, audit), FastAPI host (JSON + SSE, session ownership, probes) |
| `packages/agentkit-guardrails` | Prompt Shields input guard (Heuristic fallback), **tool-output injection shield**, PII redaction before the model, tool allow/deny/validators, per-session token budget |
| `packages/agentkit-telemetry` | One-call OTel bootstrap (OTLP / App Insights), span processor that stamps user (pseudonymized), session, tenant and team on **every** MAF span, run metrics by outcome |
| `packages/agentkit-tools` | `openapi_tools()` (OpenAPI → typed MAF tools, read-only by default), `ManagedIdentityAuth` / secretless `OnBehalfOfAuth`, `ApiClient` (safe retries, `Retry-After`, tracing, model-friendly errors), `Shaper`, `gateway_mcp_tool()`, `mock_api` for tests |
| `packages/agentkit-channels` | **Operations console** at `/console` (overview, playground, approvals, evals, security posture read from the running agent, deployments, create agent; real data only), **Microsoft Teams** (M365 Agents SDK: JWT-validated `/api/messages`, background turns and proactive replies, **approvals as Adaptive Cards** in an approvers channel with Entra-group approvers), **AG-UI** endpoint with approvals as interrupts, drop-in **web chat** (`/chat`, `<agentkit-chat>`), `TeamsTestClient` for offline Teams tests |
| `packages/agentkit-knowledge` | `knowledge_tool()`: hybrid + semantic Azure AI Search **as the signed-in user** (Entra groups via Graph, nested, fail closed), numbered sources and **citations in every channel**; `agentkit-ingest` (folder or Blob, `acl.yaml`, Document Intelligence, heading-aware chunks, gateway embeddings, incremental sync); fake index that evaluates the security filter |
| `packages/agentkit-testing` | `ScriptedChatClient` (real MAF layer stack, scripted model), span recorder, YAML eval cases that run offline in CI and live against the gateway, **LLM judge + `agentkit-gate` quality gate** (repetitions, baseline, run-page report), pytest plugin |
| `template/` + `copier.yml` | Service scaffold: agent, tools, instructions, charter, evals, tests, Dockerfile, CI and deploy callers, **`azure.yaml` + `infra/` (Bicep) for `azd up`**, `AGENTS.md`/`CLAUDE.md` |
| `infra/platform/` | Shared platform, deployed once per environment: API Management AI gateway (Entra-only, per-identity token limits, chargeback metrics), Azure OpenAI behind a managed identity, Content Safety, Container Apps environment, registry, App Insights |
| `console/` | Source of the console (React, TypeScript, Tailwind). The build is committed into `agentkit-channels`; CI checks it matches |
| `examples/order-status-agent` | A generated service after a team customized it (see its git history for the diff a team writes) |
| `.github/workflows/live-validation.yml` | Manual: deploys the platform and the sample to a throwaway environment in your subscription (what-if first), runs live checks, the live gate and judge calibration, runs every dashboard/alert query, tears down |
| `infra/platform/ops/` | The operations workbook and five alerts across every agent (spend, injection spikes, errors, approval backlog, knowledge failing closed), generated from one query file |
| `.github/workflows/agent-ci.yml`, `agent-deploy.yml` | Reusable pipelines every generated service calls: test + build; OIDC `azd up` + smoke + live evals |
| `skills/agentkit/SKILL.md` | Org skill so coding assistants write code the paved-road way |
| `scripts/e2e_smoke.py` | Boots the sample and a fake gateway with uvicorn and checks the whole HTTP path |
| `scripts/check_infra.py`, `platform_env.py` | Offline infra validation (Bicep, azd schema, actionlint, platform↔service contract); platform outputs → `azd env` / GitHub variables |



```
copier copy gh:bensunder/maf-golden-path my-agent     # new service in ~1 minute
cd my-agent && pip install -e ".[dev]" && pytest       # green offline, no model needed
```

**New here?** Read [docs/why-agentkit.md](docs/why-agentkit.md) (what it saves you), then [docs/getting-started.md](docs/getting-started.md) (your first agent in 15 minutes). Full docs: [docs/](docs/README.md).

# Develop the kit
<img width="1136" height="943" alt="image" src="https://github.com/user-attachments/assets/6d6e04d4-ff27-4fea-91d6-de18a474f2e2" />


# 🔐 Enterprise controls are part of the path
Identity & Access
Entra-aware authentication
Managed identity in production
Secretless enterprise API access
User/session ownership checks
Permission-aware document retrieval
Security
Prompt Shields with fail-closed behavior
Indirect prompt-injection protection on tool output
PII redaction before model calls and stored history
Tool allow/deny policies
Argument validation
Per-session token budgets
Human Control
Approval-required tools
Confirmation or separation of duties
Entra app-role based approvers
Approval audit trail
Approval support across API, Teams and AG-UI
Reliability & Operations
Durable session abstractions
Cross-replica locking
OpenTelemetry
Production probes
Spend / error / security / approval / knowledge alerts
Quality gates before deployment

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
| Company documents | Search trimmed to the caller's groups, citations in the API, web chat and Teams, ingestion on deploy, per-service Search + Document Intelligence (Entra only, service reads only), `cites:` / `must_not_retrieve:` evals | `knowledge`, `template/infra` |
| Operating it | A console at `/console` per service: health, quality gate, approvals, sessions, posture from the live middleware stack, deploy metadata | `channels.Console` |
| Reaching users | Teams (secretless Azure Bot, app package script) and a web chat at `/chat`, from two copier answers; every channel shares sessions, approvals and audit through one `ConversationService` | `channels`, `template/infra` |
| Model access at org level | AI gateway: Entra-only, per-identity token limits, chargeback metrics, models behind a managed identity | `infra/platform` |
| Calling enterprise APIs | Tools generated from OpenAPI; managed-identity or secretless on-behalf-of auth; safe retries, tracing, response shaping | `tools` |

## Prod policy (enforced at startup, not by review)

`AGENTKIT_ENVIRONMENT=prod` refuses to start unless: `auth_mode=managed_identity`, a gateway endpoint is set, `guardrail_mode=prompt_shields` with a Content Safety endpoint, `require_user=true`, and message-content capture is off.

## Develop the kit

```
python -m venv .venv && . .venv/bin/activate
make install     # editable installs of all packages + sample
make browser     # optional: Playwright + Chromium, for the web chat browser tests
make test        # packages, sample, template (both modes, with and without Teams), infra, end-to-end smoke
```

Status: **v0.8.0, milestone 8**: the console. Every service with web chat serves an operations console at `/console`: overview, agent detail, playground with tool traces and approvals, approvals queue, evaluations, knowledge, sessions, telemetry, security posture read from the running agent, deployments, and a guided create-agent flow. It shows only what the service knows and says where the rest lives. Earlier: **v0.7.0**: prove it in Azure. A manual live-validation workflow for your subscription, an operations dashboard and alerts, and judge calibration. The workflow is built and linted, and its check script runs in every smoke test, but it hasn't been run against a subscription yet. Earlier: **v0.6.0**: knowledge. Answers from company documents, searched as the signed-in user (Entra groups, fail closed), with citations in every channel, an ingestion pipeline with access rules, and permission-aware evals. Tested offline, including the real search SDK's wire format; not yet run against a live Azure AI Search service. Earlier: Teams and web chat channels (v0.5), the deploy quality gate (v0.4), shared sessions and human approvals (v0.3), deploy and connectors (v0.2). Not yet included: Teams SSO for on-behalf-of tools, Microsoft 365 Copilot publishing, and the .NET track. See `docs/UPGRADING.md` for the MAF version policy.
