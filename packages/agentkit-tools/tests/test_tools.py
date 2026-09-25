import json
from pathlib import Path

import httpx
import pytest
from agent_framework import Agent
from opentelemetry import trace

from agentkit.telemetry import run_context
from agentkit.testing import ScriptedChatClient, reply, tool_call
from agentkit.tools import (
    ApiClient,
    ApiKeyAuth,
    BearerTokenAuth,
    DynamicAuth,
    ManagedIdentityAuth,
    NoAuth,
    OnBehalfOfAuth,
    RetryPolicy,
    Shaper,
    ToolHttpError,
    gateway_mcp_tool,
    openapi_operations,
    openapi_tools,
)

SPEC = Path(__file__).parent / "carrier-api.yaml"


def mock_client(handler, **kwargs) -> tuple[ApiClient, list]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    client = ApiClient(
        "https://carrier.example.com/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wrapped)),
        sleep=fake_sleep,
        **kwargs,
    )
    client.sleeps = sleeps  # type: ignore[attr-defined]
    return client, seen


# --- OpenAPI → tools --------------------------------------------------------------------------


def test_read_only_by_default():
    client, _ = mock_client(lambda r: httpx.Response(200, json={}))
    names = [t.name for t in openapi_tools(SPEC, client=client)]
    assert names == ["get_shipment", "list_shipments"]


def test_write_requires_explicit_opt_in():
    client, _ = mock_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="allow_writes"):
        openapi_tools(SPEC, client=client, operations=["redirectShipment"])
    tools = openapi_tools(SPEC, client=client, operations=["redirectShipment"], allow_writes=["redirectShipment"])
    assert "(This changes data.)" in tools[0].description
    assert tools[0].additional_properties["agentkit.writes"] is True


def test_unknown_operations_are_reported():
    client, _ = mock_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="available"):
        openapi_tools(SPEC, client=client, operations=["getShipmnt"])


def test_schema_generation():
    ops = {o.operation_id: o for o in openapi_operations(SPEC)}
    get = ops["getShipment"].json_schema()
    assert get["required"] == ["trackingNumber"]  # path-level parameter inherited
    assert get["properties"]["include"]["enum"] == ["events", "dimensions"]
    assert "X-Request-Source" not in get["properties"]  # header params are the client's job
    post = ops["redirectShipment"].json_schema()
    body = post["properties"]["body"]
    assert post["required"] == ["trackingNumber", "body"]
    assert body["required"] == ["line1", "city", "postalCode"]  # $ref resolved
    assert "id" not in body["properties"]  # readOnly dropped
    assert "example" not in body["properties"]["postalCode"]  # OpenAPI-only keys dropped


async def test_tool_call_builds_request_and_shapes_response():
    def handler(request):
        return httpx.Response(200, json={"trackingNumber": "1Z 99/A", "status": "in_transit",
                                         "destination": {"city": "Lehi", "line1": "1 Main", "postalCode": "84043"},
                                         "internalRouting": "x" * 5000})

    client, seen = mock_client(handler)
    shaper = Shaper(fields=["status", "destination.city"])
    (get_shipment,) = openapi_tools(SPEC, client=client, operations=["getShipment"], shapers={"getShipment": shaper})
    result = await get_shipment.invoke(arguments={"trackingNumber": "1Z 99/A", "include": "events"})
    text = result[0].text if isinstance(result, list) else str(result)
    assert json.loads(text) == {"status": "in_transit", "destination": {"city": "Lehi"}}
    assert seen[0].url.raw_path.split(b"?")[0] == b"/v1/shipments/1Z%2099%2FA"  # path params are escaped
    assert seen[0].url.params["include"] == "events"


async def test_write_sends_json_body():
    client, seen = mock_client(lambda r: httpx.Response(202, json={"accepted": True}))
    (redirect,) = openapi_tools(SPEC, client=client, operations=["redirectShipment"], allow_writes=["redirectShipment"])
    await redirect.invoke(arguments={"trackingNumber": "T1", "body": {"line1": "2 Oak", "city": "Provo", "postalCode": "84601"}})
    assert seen[0].method == "POST"
    assert json.loads(seen[0].content) == {"line1": "2 Oak", "city": "Provo", "postalCode": "84601"}


async def test_generated_tool_inside_an_agent():
    client, seen = mock_client(lambda r: httpx.Response(200, json={"status": "delivered"}))
    tools = openapi_tools(SPEC, client=client, operations=["getShipment"])
    chat = ScriptedChatClient(script=[tool_call("get_shipment", trackingNumber="T9"), reply("Delivered.")])
    result = await Agent(chat, tools=tools).run("where is T9?")
    assert result.text == "Delivered."
    assert "delivered" in str(list(chat.tool_results().values())[0])
    assert seen[0].url.path == "/v1/shipments/T9"


# --- HTTP resilience ------------------------------------------------------------------------------


async def test_retries_idempotent_calls_and_honours_retry_after():
    responses = iter([httpx.Response(503), httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={"ok": 1})])
    client, seen = mock_client(lambda r: next(responses))
    assert await client.request("GET", "/x") == {"ok": 1}
    assert len(seen) == 3
    assert client.sleeps[1] == 2.0


async def test_does_not_retry_writes():
    client, seen = mock_client(lambda r: httpx.Response(503))
    with pytest.raises(ToolHttpError, match="503"):
        await client.request("POST", "/x", json={})
    assert len(seen) == 1


async def test_errors_are_model_friendly():
    client, _ = mock_client(lambda r: httpx.Response(404, json={"error": {"message": "no such shipment"}}))
    with pytest.raises(ToolHttpError) as err:
        await client.request("GET", "/x")
    assert str(err.value) == "HTTP 404: not found (no such shipment)"
    (tool_,) = openapi_tools(SPEC, client=client, operations=["getShipment"])
    out = await tool_.invoke(arguments={"trackingNumber": "T1"})
    assert "Error calling getShipment: HTTP 404: not found" in str(out[0].text if isinstance(out, list) else out)


async def test_timeouts_become_messages():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    client, seen = mock_client(boom, retry=RetryPolicy(attempts=2))
    with pytest.raises(ToolHttpError, match="timed out"):
        await client.request("GET", "/x")
    assert len(seen) == 2


async def test_trace_context_is_propagated():
    from opentelemetry.sdk.trace import TracerProvider

    from agentkit.testing import install_span_recorder

    install_span_recorder()
    client, seen = mock_client(lambda r: httpx.Response(200, json={}))
    with trace.get_tracer("t").start_as_current_span("tool"):
        await client.request("GET", "/x")
    assert seen[0].headers["traceparent"].startswith("00-")
    assert isinstance(trace.get_tracer_provider(), TracerProvider)


# --- auth -------------------------------------------------------------------------------------------


class FakeToken:
    def __init__(self, token):
        self.token = token


class FakeCredential:
    def __init__(self, token="mi-token"):
        self.scopes = []
        self._token = token

    async def get_token(self, scope):
        self.scopes.append(scope)
        return FakeToken(self._token)


async def test_managed_identity_auth():
    cred = FakeCredential()
    client, seen = mock_client(lambda r: httpx.Response(200, json={}), auth=ManagedIdentityAuth("api://carrier/.default", credential=cred))
    await client.request("GET", "/x")
    assert seen[0].headers["Authorization"] == "Bearer mi-token"
    assert cred.scopes == ["api://carrier/.default"]


async def test_on_behalf_of_uses_callers_token_from_run_context():
    made = {}

    def factory(user_assertion):
        made["assertion"] = user_assertion
        return FakeCredential(token=f"obo-for-{user_assertion[-4:]}")

    auth = OnBehalfOfAuth("api://carrier/.default", credential_factory=factory)
    client, seen = mock_client(lambda r: httpx.Response(200, json={}), auth=auth)
    with run_context(user_id="ben", user_assertion="user-jwt-1234"):
        await client.request("GET", "/x")
    assert made["assertion"] == "user-jwt-1234"
    assert seen[0].headers["Authorization"] == "Bearer obo-for-1234"


async def test_on_behalf_of_without_user_token_is_a_clear_tool_error():
    auth = OnBehalfOfAuth("api://carrier/.default", credential_factory=lambda a: FakeCredential())
    client, seen = mock_client(lambda r: httpx.Response(200, json={}), auth=auth)
    (tool_,) = openapi_tools(SPEC, client=client, operations=["getShipment"])
    out = await tool_.invoke(arguments={"trackingNumber": "T1"})
    assert "could not authenticate" in str(out[0].text if isinstance(out, list) else out)
    assert seen == []  # never called the API anonymously


async def test_api_key_auth():
    client, seen = mock_client(lambda r: httpx.Response(200, json={}), auth=ApiKeyAuth("k1", header="x-key"))
    await client.request("GET", "/x")
    assert seen[0].headers["x-key"] == "k1"


# --- shaping ------------------------------------------------------------------------------------------


def test_shaper_lists_wrappers_and_budget():
    data = {"value": [{"id": i, "status": "ok", "blob": "z" * 100} for i in range(50)], "@odata.count": 50}
    text = Shaper(fields=["id", "status"], items_key="value", max_items=3).shape(data)
    body, note = text.split("\n")
    assert json.loads(body) == [{"id": 0, "status": "ok"}, {"id": 1, "status": "ok"}, {"id": 2, "status": "ok"}]
    assert "showing 3 of 50" in note
    long = Shaper(max_chars=50).shape({"a": "x" * 500})
    assert len(long.split("\n")[0]) == 50 and "truncated" in long


def test_shaper_drop_paths():
    out = json.loads(Shaper(drop=["customer.ssn", "internal"]).shape({"customer": {"name": "A", "ssn": "1"}, "internal": 1, "id": 7}))
    assert out == {"customer": {"name": "A"}, "id": 7}


# --- MCP -----------------------------------------------------------------------------------------------


async def test_dynamic_auth_fetches_fresh_headers_per_request():
    calls = iter(["t1", "t2"])
    seen = []
    client = httpx.AsyncClient(
        auth=DynamicAuth(BearerTokenAuth(lambda: _next(calls))),
        transport=httpx.MockTransport(lambda r: (seen.append(r), httpx.Response(200))[1]),
    )
    await client.get("https://apim.example.com/mcp/kb")
    await client.get("https://apim.example.com/mcp/kb")
    assert [r.headers["Authorization"] for r in seen] == ["Bearer t1", "Bearer t2"]


def test_gateway_mcp_tool_wiring():
    mcp = gateway_mcp_tool("kb", "https://apim.example.com/mcp/kb", auth=NoAuth(), agent_name="orders",
                           team="commerce", allowed_tools=["search"])
    client = _find_httpx_client(mcp)
    assert isinstance(client._auth, DynamicAuth)
    assert client.headers["x-agentkit-team"] == "commerce" and client.headers["x-agentkit-agent"] == "orders"
    assert list(mcp.allowed_tools) == ["search"]


async def _next(it):
    return next(it)


def _find_httpx_client(obj):
    for value in vars(obj).values():
        if isinstance(value, httpx.AsyncClient):
            return value
    raise AssertionError("no httpx client found on MCP tool")
