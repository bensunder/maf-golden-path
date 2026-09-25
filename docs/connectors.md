# Connectors: calling enterprise APIs from tools

Most real tools call an existing API. The plumbing around each call is where tool-building time goes:

- auth: the service's identity, or the user's;
- retries that don't double-charge a customer;
- timeouts;
- trace propagation;
- error messages the model can act on;
- cutting a 200 KB response down to what the model needs.

`agentkit-tools` does all of it. You pick operations and fields.

## From OpenAPI spec to tools

```python
# src/<pkg>/connectors.py
from importlib.resources import files
from agentkit.tools import ApiClient, ManagedIdentityAuth, Shaper, openapi_tools

CARRIER = ApiClient(
    "https://carrier.example.com/v1",
    auth=ManagedIdentityAuth("api://carrier-tracking/.default"),
    timeout=10,
)

CARRIER_TOOLS = openapi_tools(
    files(__package__) / "specs" / "carrier-api.yaml",
    client=CARRIER,
    operations=["getShipment"],
    shapers={"getShipment": Shaper(fields=["trackingNumber", "status", "destination.city"])},
)
```

```python
# src/<pkg>/tools.py
from .connectors import CARRIER_TOOLS
TOOLS = [lookup_order, issue_refund, escalate_to_human, *CARRIER_TOOLS]
```

This is the order-status sample's real connector. For each selected operation you get a MAF tool with:

| From the spec | Becomes |
|---|---|
| `operationId: getShipment` | Tool name `get_shipment` |
| `summary`, `description` | Tool description (writes get "(This changes data.)" appended) |
| Path and query parameters (including path-level ones), types, enums, descriptions | The JSON schema the model sees, with required fields enforced before your call runs |
| JSON request body (`$ref`s resolved) | A `body` argument; `readOnly` properties and OpenAPI-only keys (`example`, `xml`…) dropped |
| Header and cookie parameters | Not exposed to the model. Auth and headers belong to `ApiClient` |

### Read-only by default

POST, PUT, PATCH and DELETE operations are skipped unless you opt in:

```python
openapi_tools(spec, client=CARRIER, operations=["getShipment", "redirectShipment"],
              allow_writes=["redirectShipment"])
```

Asking for a write in `operations` without `allow_writes` is an error at startup, so nobody exposes a write by accident. Pair every exposed write with a `TOOL_POLICY` validator (see [guardrails.md](guardrails.md#tool-policy-business-rules-the-model-cant-talk-its-way-around)).

## Auth

| Class | Use when | Token |
|---|---|---|
| `ManagedIdentityAuth(scope)` | The API trusts the *service* (reference data, shared lookups) | The service's managed identity (`AZURE_CLIENT_ID` is set by the template's Bicep); `az login` locally |
| `OnBehalfOfAuth(scope, client_id=…, tenant_id=…)` | Results must respect *the user's* permissions (their mailbox, their accounts, row-level security) | The caller's token, exchanged via OAuth on-behalf-of |
| `ApiKeyAuth(key, header=…)` | Third-party APIs that only take keys | Read the key from Key Vault at startup; never commit it |
| `BearerTokenAuth(async_fn)` | Anything custom | Whatever `async_fn` returns |

### On-behalf-of: the agent can't see more than the user can

1. The caller signs in to your service's Entra app registration. Easy Auth validates the token, and the agentkit host passes it (from `Authorization`) into the request context. It never goes into logs, spans or responses.
2. `OnBehalfOfAuth` exchanges it for a token to the downstream API, using the same app registration (`client_id`).
3. The service proves it *is* that app with its **managed identity as a federated credential**, so there is no client secret to store or rotate.

One-time setup:

```bash
# on the service's app registration (the one in AGENTKIT_AUTH_CLIENT_ID)
az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "service-mi", "issuer": "https://login.microsoftonline.com/<tenant-id>/v2.0",
  "subject": "<managed identity principal id>", "audiences": ["api://AzureADTokenExchange"] }'
# and grant it delegated permission to the downstream API (API permissions → admin consent)
```

```python
MAIL = ApiClient("https://graph.microsoft.com/v1.0",
                 auth=OnBehalfOfAuth("https://graph.microsoft.com/.default",
                                     client_id=os.environ["AGENTKIT_AUTH_CLIENT_ID"],
                                     tenant_id=os.environ["AZURE_TENANT_ID"],
                                     managed_identity_client_id=os.environ["AZURE_CLIENT_ID"]))
```

If a request arrives without a user token, for example from a batch job, the tool returns *"could not authenticate: this action needs the signed-in user's token"* to the model and **never calls the API anonymously**.

## What `ApiClient` does on every call

| Behaviour | Detail |
|---|---|
| Retries | 3 attempts for idempotent methods (GET, HEAD, OPTIONS, PUT, DELETE) on 408/429/5xx, timeouts and connection errors. Exponential backoff with jitter; honours `Retry-After` (seconds or HTTP date), capped at 8s |
| No retry for POST/PATCH | A retried "create refund" could refund twice. Make those idempotent server-side if you need retries |
| Timeouts | 15s default, per client |
| Tracing | W3C `traceparent` injected, so the downstream API's spans join the agent's trace in App Insights |
| Errors | Turned into one line the model can use: `HTTP 404: not found (no such shipment)`, `the service timed out`, `HTTP 403: access denied (the caller lacks permission)`. No stack traces, no response dumps |

Share one `ApiClient` per API across its tools, for connection pooling.

## Shaping responses

```python
Shaper(fields=["id", "status", "customer.name"])      # keep only these (dot paths)
Shaper(drop=["customer.ssn", "internal"])              # or remove these
Shaper(items_key="value", max_items=10)                # lists (OData "value", "items"…): cap and say so
Shaper(max_chars=4000)                                 # hard budget (the default)
```

When the shaper truncates, it tells the model: `[showing 10 of 250 results; ask for a narrower query to see others]`. `fields` is also a data-minimisation control. In the sample, the carrier's internal routing code and street address never reach the model.

## MCP servers

```python
from agentkit.tools import ManagedIdentityAuth, gateway_mcp_tool

KB = gateway_mcp_tool(
    "knowledge-base",
    "https://apim-ai.contoso.com/mcp/kb",
    auth=ManagedIdentityAuth("api://kb-mcp/.default"),
    agent_name="order-status", team="commerce",
    allowed_tools=["search_articles"],          # always narrow what the agent sees
)
TOOLS = [..., KB]
```

MAF's MCP tool takes static headers, which go stale when the token expires, usually within about an hour. `gateway_mcp_tool` instead gives it an HTTP client that fetches a fresh token on every request, and adds the `x-agentkit-*` headers. Put MCP servers behind API Management like models, so they share auth, logging and rate limits.

## Testing connectors

Never call real APIs from unit tests. `mock_api` swaps a client's transport and auth for a test:

```python
from agentkit.tools.testing import mock_api

with mock_api(CARRIER, {"GET /shipments/1Z999": {"status": "delivered"},
                        "GET /shipments/BAD": (404, {"message": "unknown"})}) as calls:
    result = await create_agent(settings, client=scripted).run("where is 1Z999?")
assert calls[0].url.path.endswith("/shipments/1Z999")
```

Make it an **autouse fixture** in `tests/conftest.py`, as the sample does, so offline evals are hermetic too. Skip the fixture when `AGENTKIT_LIVE_EVALS=1`, so live evals hit the real API.

## When not to generate

- The API has no spec, or the spec is poor: write a normal `@tool` and call `ApiClient.request(...)` inside it. You still get auth, retries, tracing and error mapping.
- The operation needs several calls composed into one (look up the customer, then their orders): write one tool that uses `ApiClient`, rather than exposing two low-level operations and hoping the model chains them.
