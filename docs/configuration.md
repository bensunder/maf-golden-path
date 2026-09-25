# Configuration reference

All settings are environment variables with the `AGENTKIT_` prefix, read by `AgentKitSettings` (pydantic-settings). A `.env` file in the working directory is also read. Code never needs endpoints, keys or limits.

## Service identity

| Variable | Default | Meaning |
|---|---|---|
| `AGENTKIT_ENVIRONMENT` | `local` | `local`, `dev`, `test` or `prod`. `prod` turns on the policy checks below |
| `AGENTKIT_SERVICE_NAME` | `agent` | `service.name` in telemetry; gateway header |
| `AGENTKIT_SERVICE_VERSION` | `0.0.0` | `service.version`; returned by `/readyz` |
| `AGENTKIT_TEAM` | `unassigned` | Owning team: span attribute and gateway quota header |

## Model access

| Variable | Default | Meaning |
|---|---|---|
| `AGENTKIT_GATEWAY_ENDPOINT` | none | AI gateway base URL, e.g. `https://apim-ai.contoso.com`. Required outside tests |
| `AGENTKIT_GATEWAY_STYLE` | `azure` | `azure` = `/openai/deployments/{model}/…`; `openai_v1` = `/openai/v1` |
| `AGENTKIT_MODEL` | `gpt-4.1-mini` | Deployment / model name |
| `AGENTKIT_API_VERSION` | `2024-10-21` | Azure OpenAI API version (azure style) |
| `AGENTKIT_AUTH_MODE` | `default` | `managed_identity`, `azure_cli`, `default` (DefaultAzureCredential) or `api_key` |
| `AGENTKIT_MANAGED_IDENTITY_CLIENT_ID` | none | For a user-assigned managed identity |
| `AGENTKIT_TOKEN_SCOPE` | `https://cognitiveservices.azure.com/.default` | Entra scope requested for the gateway |
| `AGENTKIT_API_KEY` | none | Only with `auth_mode=api_key` (local / fake gateway) |
| `AGENTKIT_GATEWAY_SUBSCRIPTION_KEY` | none | APIM subscription key for the team's product |

## Run limits

| Variable | Default | Meaning |
|---|---|---|
| `AGENTKIT_MAX_ITERATIONS` | `8` | Max model round-trips in one tool loop |
| `AGENTKIT_MAX_FUNCTION_CALLS` | `20` | Max tool executions per run |
| `AGENTKIT_MAX_RUN_SECONDS` | `120` | Max duration of the tool loop; also the HTTP client timeout |
| `AGENTKIT_SESSION_TOKEN_BUDGET` | `200000` | Total tokens per session before further requests are refused |
| `AGENTKIT_SESSION_TTL_SECONDS` | `3600` | Session lifetime since last use |
| `AGENTKIT_MAX_INPUT_CHARS` | `20000` | Longer user messages are refused before any call |

The first three map to MAF's `FunctionInvocationConfiguration`. Team-wide quotas belong on the gateway (APIM `llm-token-limit`), not here.

## Guardrails

| Variable | Default | Meaning |
|---|---|---|
| `AGENTKIT_GUARDRAIL_MODE` | `heuristic` | `prompt_shields` (Azure AI Content Safety), `heuristic` (offline regex), or `off` |
| `AGENTKIT_CONTENT_SAFETY_ENDPOINT` | none | Required for `prompt_shields` |
| `AGENTKIT_CONTENT_SAFETY_KEY` | none | Optional; Entra ID (same credential as the gateway) is used when unset |
| `AGENTKIT_SHIELDS_FAIL_CLOSED` | `true` | If Prompt Shields is unreachable, refuse (`true`) or allow (`false`) |
| `AGENTKIT_REDACT_PII` | `true` | Redact email, SSN, card and phone numbers in user text before the model |
| `AGENTKIT_SCAN_TOOL_OUTPUT` | `true` | Scan tool results for indirect prompt injection |

## Telemetry

| Variable | Default | Meaning |
|---|---|---|
| `AGENTKIT_OTLP_ENDPOINT` | none | OTLP collector, e.g. the Aspire dashboard at `http://localhost:4317` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` (or `AGENTKIT_APPINSIGHTS_CONNECTION_STRING`) | none | Sends traces, metrics and logs to Azure Monitor |
| `AGENTKIT_CAPTURE_MESSAGE_CONTENT` | `false` | Record prompts and responses on spans. Local debugging only; forbidden in prod |

## HTTP host

| Variable | Default | Meaning |
|---|---|---|
| `AGENTKIT_USER_HEADER` | `x-ms-client-principal-name` | Header carrying the authenticated caller (set by platform auth) |
| `AGENTKIT_TENANT_HEADER` | `x-agentkit-tenant` | Header carrying the tenant id |
| `AGENTKIT_REQUIRE_USER` | `false` | Reject requests without the user header (`401`) |
| `AGENTKIT_USER_TOKEN_HEADER` | `authorization` | Header with the caller's own bearer token. It's passed privately to on-behalf-of tools (`agentkit.tools.OnBehalfOfAuth`) and never logged. Set to empty to disable |

## Set by the deploy (you don't set these)

The generated `infra/main.bicep` sets these on the Container App from the platform outputs: `AGENTKIT_ENVIRONMENT`, `AGENTKIT_SERVICE_NAME`, `AGENTKIT_TEAM`, `AGENTKIT_GATEWAY_ENDPOINT`, `AGENTKIT_MODEL`, `AGENTKIT_AUTH_MODE=managed_identity`, `AGENTKIT_MANAGED_IDENTITY_CLIENT_ID`, `AZURE_CLIENT_ID`, `AGENTKIT_GUARDRAIL_MODE=prompt_shields`, `AGENTKIT_CONTENT_SAFETY_ENDPOINT`, `AGENTKIT_REQUIRE_USER=true` and `APPLICATIONINSIGHTS_CONNECTION_STRING` (as a secret). To add your own (for example `CARRIER_API_URL`), extend the `env` list in `infra/main.bicep`.

## Prod policy

With `AGENTKIT_ENVIRONMENT=prod`, the service **refuses to start** unless all of these hold:

| Rule | Why |
|---|---|
| `AUTH_MODE=managed_identity` | No keys or developer credentials in production |
| `GATEWAY_ENDPOINT` set | All model traffic goes through the gateway (quotas, chargeback, content safety) |
| `GUARDRAIL_MODE=prompt_shields` (with `CONTENT_SAFETY_ENDPOINT`) | The heuristic detector is not a production defence |
| `CAPTURE_MESSAGE_CONTENT=false` | Prompts can contain personal data |
| `REQUIRE_USER=true` | Sessions and traces must be attributable to a caller |

It fails at startup with one error listing every violation, so misconfiguration never reaches traffic. The generated `Dockerfile` defaults to `prod`, `managed_identity` and `require_user=true`.

Two rules apply in every environment: `prompt_shields` needs `CONTENT_SAFETY_ENDPOINT`, and `api_key` auth needs `API_KEY`.

## Typical setups

**Unit tests:** nothing to set. The generated `conftest.py` builds settings with `environment="test"` and no `.env`.

**Local, fake gateway:**
```bash
AGENTKIT_GATEWAY_ENDPOINT=http://127.0.0.1:9100 AGENTKIT_AUTH_MODE=api_key AGENTKIT_API_KEY=local
```

**Local, real gateway:**
```bash
AGENTKIT_GATEWAY_ENDPOINT=https://apim-ai.contoso.com AGENTKIT_AUTH_MODE=azure_cli   # after az login
```

**Production (Container Apps):**
```bash
AGENTKIT_ENVIRONMENT=prod
AGENTKIT_AUTH_MODE=managed_identity
AGENTKIT_MANAGED_IDENTITY_CLIENT_ID=<uami client id>
AGENTKIT_GATEWAY_ENDPOINT=https://apim-ai.contoso.com
AGENTKIT_GUARDRAIL_MODE=prompt_shields
AGENTKIT_CONTENT_SAFETY_ENDPOINT=https://cs-agents.cognitiveservices.azure.com
AGENTKIT_REQUIRE_USER=true
APPLICATIONINSIGHTS_CONNECTION_STRING=<from Key Vault reference>
```
