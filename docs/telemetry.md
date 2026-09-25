# Telemetry

`create_app` calls `setup_telemetry` at startup from your settings. In a service you don't call anything.

## What you get

**Traces** follow the OpenTelemetry GenAI conventions and come from MAF itself:

```
invoke_agent order-status          gen_ai.agent.name, gen_ai.usage.input_tokens/output_tokens
├── chat gpt-4.1-mini              gen_ai.operation.name=chat, token usage
├── execute_tool lookup_order      duration
└── chat gpt-4.1-mini
```

**agentkit adds these attributes to every one of those spans**, including the chat and tool spans deep inside MAF's tool loop:

| Attribute | Source |
|---|---|
| `enduser.pseudo.id` | Caller identity, SHA-256 pseudonymized (the raw id never leaves the process) |
| `session.id` | Session id |
| `agentkit.tenant.id` | Tenant header |
| `agentkit.request.id` | Per-request id |
| `agentkit.team` | `AGENTKIT_TEAM` |

Resource attributes: `service.name`, `service.version`, `deployment.environment.name`, `agentkit.team`.

**Metrics:**

| Metric | Attributes | Use |
|---|---|---|
| `agentkit.agent.runs` | `gen_ai.agent.name`, `outcome` (`ok` / `blocked` / `error`), `stream` | Block rate, error rate |
| `agentkit.agent.run.duration` | same | Latency SLOs |
| MAF's GenAI token and duration metrics | model, operation | Token spend |

## Where it goes

| Setting | Destination |
|---|---|
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Azure Monitor / Application Insights (traces, metrics, logs) |
| `AGENTKIT_OTLP_ENDPOINT` | Any OTLP collector. Locally, the .NET Aspire dashboard: `docker run -p 18888:18888 -p 4317:18889 mcr.microsoft.com/dotnet/aspire-dashboard` |
| neither | Nothing is exported (tests, quick local runs) |

## Why a span processor, not middleware

MAF's agent middleware runs *outside* the `invoke_agent` span. Attributes set on the "current span" from middleware land on your web framework's span, or nowhere, and never on the chat and tool spans. agentkit's `RunContextSpanProcessor` stamps context on every span as it starts. `create_app` sets the context per request with `run_context(...)`.

Outside the HTTP host (a queue worker, a batch job), set it yourself:

```python
from agentkit.telemetry import run_context

with run_context(user_id=msg.user, session_id=msg.conversation_id, tenant_id=msg.tenant, channel="servicebus"):
    await agent.run(msg.text, session=session)
```

Extra keyword arguments become `agentkit.<key>` attributes.

## Prompt and response content

Off by default. `AGENTKIT_CAPTURE_MESSAGE_CONTENT=true` adds message content to spans for local debugging. The setting is **rejected in prod**, because prompts contain customer data.

## Useful queries (Application Insights, KQL)

Block rate by agent over the last day:

```kusto
customMetrics
| where name == "agentkit.agent.runs" and timestamp > ago(1d)
| extend agent = tostring(customDimensions["gen_ai.agent.name"]), outcome = tostring(customDimensions["outcome"])
| summarize runs = sum(valueSum) by agent, outcome
```

Everything that happened in one conversation:

```kusto
dependencies
| where customDimensions["session.id"] == "<session id>"
| project timestamp, name, duration, customDimensions
| order by timestamp asc
```
