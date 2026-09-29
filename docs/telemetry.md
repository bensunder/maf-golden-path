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
| `LANGSMITH_API_KEY` | [LangSmith](#langsmith), alongside either of the above |
| none of these | Nothing is exported (tests, quick local runs) |

### LangSmith

Install `agentkit-telemetry[langsmith]` (the generated `pyproject.toml` and the VPS image already do) and set `LANGSMITH_API_KEY`. Spans go to LangSmith's OpenTelemetry endpoint as well as to any other destination. They land in the project `LANGSMITH_PROJECT`, or the service name if that's unset. Agent, model and tool spans carry `langsmith.span.kind`, so LangSmith shows them as chain, LLM and tool runs. MAF and [LangGraph](langgraph.md) agents look the same there, and a request that goes through [several agents](multi-agent.md) is one trace across all of them.

EU region: `LANGSMITH_OTEL_ENDPOINT=https://eu.api.smith.langchain.com/otel`. Self-hosted: `https://<your host>/api/v1/otel`. Message content follows `AGENTKIT_CAPTURE_MESSAGE_CONTENT`, like every other destination.

To record eval results in LangSmith as well, see [testing-and-evals.md](testing-and-evals.md#langsmith).

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
