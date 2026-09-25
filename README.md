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
| `packages/agentkit-hosting` | `build_agent()`, gateway-bound client (APIM, Azure or OpenAI-v1 style), Entra credential per environment, `AgentKitSettings` with **prod policy enforcement**, session store, FastAPI host (JSON + SSE, session ownership, probes) |
| `packages/agentkit-guardrails` | Prompt Shields input guard (Heuristic fallback), **tool-output injection shield**, PII redaction before the model, tool allow/deny/validators, per-session token budget |
| `packages/agentkit-telemetry` | One-call OTel bootstrap (OTLP / App Insights), span processor that stamps user (pseudonymized), session, tenant and team on **every** MAF span, run metrics by outcome |
| `packages/agentkit-testing` | `ScriptedChatClient` (real MAF layer stack, scripted model), span recorder, YAML eval cases that run offline in CI and live against the gateway, pytest plugin |
| `template/` + `copier.yml` | Service scaffold: agent, tools, instructions, charter, evals, tests, Dockerfile, CI caller, `AGENTS.md`/`CLAUDE.md` |
| `examples/order-status-agent` | A generated service after a team customized it (see its git history for the diff a team writes) |
| `.github/workflows/agent-ci.yml` | Reusable pipeline every generated service calls |
| `skills/agentkit/SKILL.md` | Org skill so coding assistants write code the paved-road way |
| `scripts/e2e_smoke.py` | Boots the sample and a fake gateway with uvicorn and checks the whole HTTP path |

## What a team writes vs. what it gets

In the sample, the team-authored code is `tools.py` (3 tools + a refund policy), `instructions/system.md` and `evals/cases.yaml`. Everything below is inherited:

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

## Prod policy (enforced at startup, not by review)

`AGENTKIT_ENVIRONMENT=prod` refuses to start unless: `auth_mode=managed_identity`, a gateway endpoint is set, `guardrail_mode=prompt_shields` with a Content Safety endpoint, `require_user=true`, and message-content capture is off.

## Develop the kit

```
python -m venv .venv && . .venv/bin/activate
make install     # editable installs of all packages + sample
make test        # packages, sample, template (both modes), end-to-end smoke
```

Status: **v0.1.0, milestone 1** (packages + template + sample). Not yet included: azd/Bicep infra, APIM policy bundle, live eval gate in the pipeline, Redis/Cosmos session store, human-approval endpoint for `approval_mode="always_require"` tools, and the .NET track. See `docs/UPGRADING.md` for the MAF version policy.
