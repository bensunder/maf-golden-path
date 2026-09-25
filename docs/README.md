# agentkit documentation

agentkit is the paved road for building agents on Microsoft Agent Framework (Python). You write tools, instructions and eval cases. The kit handles model access, identity, guardrails, telemetry, sessions, the HTTP API, tests and CI.

| Start here | |
|---|---|
| [Why agentkit](why-agentkit.md) | What it saves you: measured code size, estimated engineering days, and the MAF pitfalls already handled |
| [Getting started](getting-started.md) | Generate a service, add a tool and a business rule, write evals, run it locally, deploy: 15 minutes |

| Reference | |
|---|---|
| [Architecture](architecture.md) | Request flow, middleware order, gateway contract, sessions, HTTP API |
| [Deploying](deploy.md) | Shared platform (AI gateway, models, safety), `azd up` per service, OIDC pipeline, offline validation, troubleshooting |
| [Sessions and approvals](sessions-and-approvals.md) | Cosmos/Redis sessions and cross-replica locking; human approval for high-impact tools (confirmation or separation of duties), audit, evals |
| [Connectors](connectors.md) | OpenAPI → tools, managed identity and on-behalf-of auth, retries, response shaping, MCP, test fakes |
| [Configuration](configuration.md) | Every `AGENTKIT_*` setting and the prod policy |
| [Guardrails](guardrails.md) | Prompt Shields, indirect injection, PII, tool policy, token budgets, custom guardrails |
| [Telemetry](telemetry.md) | Spans, attributes, metrics, destinations, KQL queries |
| [Testing and evals](testing-and-evals.md) | `ScriptedChatClient`, eval case format, offline vs live |
| [FAQ and escape hatches](faq.md) | Stepping off the paved road safely |
| [Upgrading](UPGRADING.md) | MAF version policy, for the platform team |
