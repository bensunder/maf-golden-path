"""The fleet view: a registry of agent services, each asked live (server-side, with the fleet's identity)."""

import base64
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from agentkit.channels import AgUiChannel, Console
from agentkit.channels.fleet import FleetAgent, FleetRegistry, create_fleet_app, discover_agents, load_registry
from agentkit.channels.testing import FakeLogs
from agentkit.hosting import AgentKitSettings, build_agent, create_app
from agentkit.testing import ScriptedChatClient

from conftest import Server

USER = {"x-ms-client-principal-name": "platform@contoso.example"}
AUD = "api://11111111-2222-3333-4444-555555555555"
AUD2 = "api://66666666-2222-3333-4444-555555555555"
FLEET_SETTINGS = AgentKitSettings(environment="test", guardrail_mode="heuristic", _env_file=None, require_user=True)


class FakeEasyAuth:
    """What Easy Auth does in front of each agent: a valid bearer token becomes identity headers
    (an app-only token has no name, only an object id, plus its app roles)."""

    def __init__(self, app, roles=()):
        self.app, self.roles = app, list(roles)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = [(k, v) for k, v in scope["headers"] if not k.startswith(b"x-ms-client-principal")]
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if auth == b"Bearer fleet-token":
                principal = {"claims": [{"typ": "roles", "val": r} for r in self.roles]}
                headers += [(b"x-ms-client-principal-id", b"fleet-identity-oid"),
                            (b"x-ms-client-principal", base64.b64encode(json.dumps(principal).encode()))]
            scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


def agent_app(name, *, console=True, roles=(), console_role=None, **settings):
    s = AgentKitSettings(**{"environment": "test", "guardrail_mode": "heuristic", "_env_file": None,
                            "service_name": name, "service_version": "2.0.1", "require_user": True, **settings})
    channels = [AgUiChannel()] + ([Console(title=name.title(), role=console_role)] if console else [])
    app = create_app(lambda s: build_agent(name=name, instructions="x", settings=s, client=ScriptedChatClient()),
                     settings=s, configure_telemetry=False, channels=channels)
    return FakeEasyAuth(app, roles)


async def token_for(audience):
    assert audience.startswith("api://")
    return "fleet-token"


def dead_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


def test_registry_file_json_and_compact_forms(tmp_path):
    f = tmp_path / "fleet.yaml"
    f.write_text(f"agents:\n  - url: https://orders.example/\n    name: Orders\n    audience: {AUD}\n")
    agents = load_registry(f, json.dumps([{"url": "https://hr.example"}]))
    assert [a.url for a in agents] == ["https://orders.example", "https://hr.example"]
    assert agents[0].name == "Orders" and agents[0].audience == AUD and agents[1].audience is None
    compact = load_registry(inline=f"https://a.example|{AUD}|Agent A; https://b.example||")
    assert [(a.url, a.audience, a.name) for a in compact] == [("https://a.example", AUD, "Agent A"),
                                                               ("https://b.example", None, None)]


@pytest.mark.parametrize("bad", [{"url": "javascript:alert(1)"}, {"url": "https://x.example", "audience": "https://management.azure.com"},
                                 {"url": "https://x.example", "audience": "https://api.loganalytics.io/.default"},
                                 {"url": "https://x.example", "audience": "api://not-a-guid"}])
def test_registry_refuses_unsafe_entries(bad):
    with pytest.raises(ValueError):
        load_registry(inline=json.dumps([bad]))


async def test_discovery_takes_the_audience_from_easy_auth_not_from_tags():
    seen = {"arg": []}
    apps = [
        {"id": "/subscriptions/s/resourceGroups/g/providers/Microsoft.App/containerApps/ca-orders", "name": "ca-orders",
         "fqdn": "ca-orders.azurecontainerapps.io", "service": "order-status-agent", "environment": "prod"},
        # a hostile app: any tag it likes, but its Easy Auth is for some other resource
        {"id": "/subscriptions/s/resourceGroups/evil/providers/Microsoft.App/containerApps/ca-evil", "name": "ca-evil",
         "fqdn": "evil.example", "service": "x", "environment": "prod"},
        {"id": "/subscriptions/s/resourceGroups/g/providers/Microsoft.App/containerApps/ca-dev", "name": "ca-dev",
         "fqdn": "ca-dev.azurecontainerapps.io", "service": "order-status-agent", "environment": "dev"},
    ]

    def handler(request):
        if request.url.path.endswith("/resources"):
            body = json.loads(request.content)
            seen["arg"].append(body)
            if "$skipToken" not in body["options"]:
                return httpx.Response(200, json={"data": apps[:1], "$skipToken": "next"})
            return httpx.Response(200, json={"data": apps[1:]})
        assert request.headers["authorization"] == "Bearer arm-token"
        if "ca-orders" in request.url.path:
            return httpx.Response(200, json={"properties": {"platform": {"enabled": True}, "identityProviders": {
                "azureActiveDirectory": {"registration": {"clientId": AUD.removeprefix("api://")}}}}})
        return httpx.Response(200, json={"properties": {"identityProviders": {
            "azureActiveDirectory": {"registration": {"clientId": "https://management.azure.com"}}}}})

    async def arm():
        return "arm-token"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        found = await discover_agents(arm, subscriptions=["sub-1"], environment="prod", client=http)
    assert found == [
        FleetAgent(url="https://ca-orders.azurecontainerapps.io", name="order-status-agent", audience=AUD, source="discovered"),
        FleetAgent(url="https://evil.example", name="x", audience=None, source="discovered"),  # never gets a token
    ]
    assert len(seen["arg"]) == 2 and seen["arg"][0]["subscriptions"] == ["sub-1"]
    assert 'tags["agentkit-service"]' in seen["arg"][0]["query"] and "agentkit-auth-audience" not in seen["arg"][0]["query"]


async def test_registry_keeps_the_last_good_discovery():
    calls = {"n": 0}

    async def discover():
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("ARM down")
        return [FleetAgent(url="https://b.example", source="discovered")]

    reg = FleetRegistry([FleetAgent(url="https://a.example"), FleetAgent(url="https://B.example/")], discover,
                        refresh_seconds=0)
    first = await reg.agents()
    second = await reg.agents()
    assert [(a.url, a.source) for a in first] == [("https://a.example", "registry"), ("https://B.example", "both")]
    assert second == first and reg.discovery_error and "last discovered" in reg.discovery_error


def test_fleet_asks_every_agent_and_reports_what_it_found():
    with Server(agent_app("orders")) as orders, Server(agent_app("hr", console=False)) as hr, \
            Server(agent_app("finance", console_role="Console.Read")) as finance, \
            Server(agent_app("legal", console_role="Console.Read", roles=["Console.Read"])) as legal:
        registry = FleetRegistry([FleetAgent(url=u, audience=AUD) for u in (orders, hr, finance, legal)]
                                 + [FleetAgent(url=dead_url(), name="Gone")])
        app = create_fleet_app(registry, settings=FLEET_SETTINGS, token_for=token_for, logs=FakeLogs())
        with TestClient(app) as http:
            assert http.get("/v1/fleet/agents").status_code == 401
            body = http.get("/v1/fleet/agents", headers=USER).json()
    by_url = {a["url"]: a for a in body["agents"]}
    o = by_url[orders]
    assert o["status"] == "ready" and o["console"] == "ok" and o["name"] == "Orders"
    assert o["overview"]["service"]["version"] == "2.0.1" and o["overview"]["agent"]["name"] == "orders"
    assert "limits" not in o["overview"]["agent"]
    assert {c["id"] for c in o["overview"]["security"]} >= {"pii", "entra_auth"}
    assert o["gate"]["cases"] == 0 and o["gate"]["report"] is None
    assert o["console_url"] == orders + "/console"
    assert by_url[hr]["status"] == "ready" and by_url[hr]["console"] == "not_installed" and by_url[hr]["overview"] is None
    assert by_url[finance]["console"] == "denied" and "console role" in by_url[finance]["detail"]
    assert by_url[legal]["console"] == "ok"  # the fleet identity holds the role
    gone = [a for a in body["agents"] if a["name"] == "Gone"][0]
    assert gone["status"] == "unreachable" and gone["overview"] is None


def test_fleet_role_and_traffic():
    logs = FakeLogs(fleet=[{"Agent": "orders", "Services": '["order-status-agent"]', "Runs": 40, "Errors": 2, "Blocked": 1,
                            "DurationSum": 60, "DurationCount": 40, "Tokens": 50000},
                           {"Agent": "hr", "Services": [], "Runs": 0, "Errors": 0, "Blocked": 0,
                            "DurationSum": 0, "DurationCount": 0, "Tokens": 10}])
    app = create_fleet_app(FleetRegistry([]), settings=FLEET_SETTINGS, token_for=token_for, logs=logs,
                           role="Fleet.Read")
    principal = base64.b64encode(json.dumps({"claims": [{"typ": "roles", "val": "Fleet.Read"}]}).encode()).decode()
    with TestClient(app) as http:
        assert http.get("/v1/fleet/traffic", headers=USER).status_code == 403
        ok = {**USER, "x-ms-client-principal": principal}
        body = http.get("/v1/fleet/traffic?range=7d", headers=ok).json()
        assert http.get("/v1/fleet/traffic?range=nope", headers=ok).status_code == 400
        page = http.get("/console/security")
    assert body["available"] and [a["agent"] for a in body["agents"]] == ["orders", "hr"]
    assert body["agents"][0]["avg_s"] == 1.5 and body["agents"][0]["error_rate"] == 0.05
    assert body["agents"][0]["services"] == ["order-status-agent"] and body["agents"][1]["services"] == []
    assert body["agents"][1]["avg_s"] is None and body["agents"][1]["error_rate"] is None
    assert logs.queries[0][1] == "P7D"
    assert page.status_code == 200 and 'content="fleet"' in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]


def test_fleet_page_in_a_browser(browser):
    with Server(agent_app("orders")) as orders, Server(agent_app("hr", console=False)) as hr:
        registry = FleetRegistry([FleetAgent(url=orders, audience=AUD), FleetAgent(url=hr, audience=AUD2),
                                  FleetAgent(url=dead_url(), name="Gone")])
        with Server(create_fleet_app(registry, settings=FLEET_SETTINGS, token_for=token_for, logs=FakeLogs())) as fleet:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.set_extra_http_headers(USER)
            page.goto(fleet + "/console")
            page.get_by_role("heading", name="Agent fleet").wait_for()
            table = page.get_by_role("table", name="Agents")
            table.get_by_text("Orders", exact=True).wait_for()
            assert "Unreachable" in table.inner_text() and "No console on this service" in table.inner_text()
            assert table.get_by_role("link", name="Console for Orders", exact=False).get_attribute("href") == orders + "/console"
            page.get_by_role("link", name="Security").click()
            page.get_by_role("table", name="Security controls by agent").wait_for()
            assert page.get_by_role("img", name="PII redaction: on", exact=False).count() == 1
            page.get_by_role("link", name="Traffic").click()
            page.get_by_text("No agent traffic in this period").wait_for()
    assert errors == []


def test_fleet_app_from_the_environment_starts_in_prod(monkeypatch):
    """The fleet runs no agent, so the agent prod policy (gateway, Prompt Shields) must not stop it."""
    import agentkit.channels.fleet as fleet

    monkeypatch.setenv("AGENTKIT_ENVIRONMENT", "prod")
    monkeypatch.setenv("AGENTKIT_AUTH_MODE", "api_key")  # no Entra identity in the test
    monkeypatch.setenv("AGENTKIT_FLEET_AGENTS", json.dumps([{"url": "https://orders.example", "name": "Orders"}]))
    monkeypatch.delenv("AGENTKIT_CONSOLE_LOGS_RESOURCE", raising=False)
    app = fleet.create_fleet_app()
    with TestClient(app) as http:
        assert http.get("/readyz").status_code == 200
        me = http.get("/v1/fleet/me", headers=USER).json()
        traffic = http.get("/v1/fleet/traffic", headers=USER).json()
    assert me["environment"] == "prod" and me["user"] == "platform@contoso.example"
    assert traffic["available"] is False and "AGENTKIT_CONSOLE_LOGS_RESOURCE" in traffic["reason"]
    assert len(app.state.fleet.registry.static) == 1


def test_a_broken_agent_never_takes_the_fleet_down_and_tokens_stay_on_https():
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    weird = FastAPI()
    weird.get("/readyz")(lambda: {"status": "ready"})
    weird.get("/v1/console/overview")(lambda: HTMLResponse("<html>not json</html>"))
    lister = FastAPI()
    lister.get("/readyz")(lambda: {"status": "ready"})
    lister.get("/v1/console/overview")(lambda: JSONResponse([1, 2, 3]))
    sent: list[str] = []

    async def recording_token(audience):
        sent.append(audience)
        return "fleet-token"

    with Server(weird) as w, Server(lister) as li, Server(agent_app("orders")) as o:
        plain = w.replace("127.0.0.1", "not-local.invalid")  # plain http, not this machine
        registry = FleetRegistry([FleetAgent(url=w, audience=AUD), FleetAgent(url=li, audience=AUD),
                                  FleetAgent(url=o, audience=AUD), FleetAgent(url=plain, audience=AUD2)])
        app = create_fleet_app(registry, settings=FLEET_SETTINGS, token_for=recording_token, logs=FakeLogs())
        with TestClient(app) as http:
            body = http.get("/v1/fleet/agents", headers=USER).json()
    by_url = {a["url"]: a for a in body["agents"]}
    assert by_url[w]["console"] == "error" and "JSON" in by_url[w]["detail"]
    assert by_url[li]["console"] == "error"
    assert by_url[o]["console"] == "ok"
    assert by_url[plain]["overview"] is None
    assert AUD2 not in sent
    # tokens only travel over https, or to this machine
    assert not FleetAgent(url="http://agent.example", audience=AUD).token_allowed
    assert FleetAgent(url="https://agent.example", audience=AUD).token_allowed
    assert FleetAgent(url="http://127.0.0.1:8000", audience=AUD).token_allowed


VPS_AGENT = {"user_header": "x-forwarded-email", "user_fallback_header": "",
             "principal_claims_header": "", "user_token_header": ""}


def test_private_network_fleet_reads_agents_with_its_caller_identity_and_links_to_public_urls(monkeypatch):
    """deploy/vps: agents trust only X-Forwarded-Email (set by oauth2-proxy, or by the fleet on the private
    network); the fleet reaches them at internal addresses but links people to their public host names."""
    from agentkit.channels.fleet import parse_caller_header

    with Server(agent_app("orders", **VPS_AGENT).app) as orders, Server(agent_app("legal", **VPS_AGENT).app) as legal:
        inline = f"{orders}||Orders|https://orders.203-0-113-7.sslip.io; {legal}||Legal|https://legal.203-0-113-7.sslip.io/"
        monkeypatch.setenv("AGENTKIT_FLEET_AGENTS", inline)
        monkeypatch.setenv("AGENTKIT_FLEET_CALLER_HEADER", "x-forwarded-email: fleet@agentkit.local")
        fleet_settings = AgentKitSettings(environment="dev", guardrail_mode="heuristic", _env_file=None, require_user=True,
                                          **VPS_AGENT)
        with TestClient(create_fleet_app(settings=fleet_settings, logs=FakeLogs())) as http:
            assert http.get("/v1/fleet/agents", headers=USER).status_code == 401  # Easy Auth headers mean nothing here
            body = http.get("/v1/fleet/agents", headers={"x-forwarded-email": "ben@contoso.example"}).json()
        monkeypatch.delenv("AGENTKIT_FLEET_CALLER_HEADER")
        with TestClient(create_fleet_app(settings=fleet_settings, logs=FakeLogs())) as http:
            without = http.get("/v1/fleet/agents", headers={"x-forwarded-email": "ben@contoso.example"}).json()
    by_name = {a["name"]: a for a in body["agents"]}
    assert by_name["Orders"]["console"] == "ok" and by_name["Legal"]["console"] == "ok"
    assert by_name["Orders"]["console_url"] == "https://orders.203-0-113-7.sslip.io/console"
    assert by_name["Legal"]["console_url"] == "https://legal.203-0-113-7.sslip.io/console"
    assert by_name["Orders"]["url"] == orders  # still probed at the private address
    assert {a["console"] for a in without["agents"]} == {"denied"}  # no caller identity: the agents refuse

    assert parse_caller_header(" X-Forwarded-Email :  fleet@x ") == ("x-forwarded-email", "fleet@x")
    for bad in ("authorization: Bearer x", "cookie: a=b", "x-ms-client-principal-name: admin", "no-colon", "bad name: x",
                "proxy-authorization: x", "x-forwarded-for: 1.2.3.4"):
        with pytest.raises(ValueError):
            parse_caller_header(bad)
    with pytest.raises(ValueError):
        load_registry(inline="http://agent:8000||Orders|javascript:alert(1)")


def test_caller_identity_is_never_sent_with_a_token():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, json={"status": "ready"})

    from agentkit.channels.fleet import FleetService

    registry = FleetRegistry([FleetAgent(url="https://a.example", audience=AUD),        # a token instead
                              FleetAgent(url="https://public.example"),                 # public: no identity
                              FleetAgent(url="http://agent.example.com:8000")])         # plain http, but public
    service = FleetService(registry, token_for,
                           client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                           caller_header=("x-forwarded-email", "fleet@agentkit.local"))
    import asyncio

    asyncio.run(service.agents())
    assert seen and all("x-forwarded-email" not in h for h in seen)
    private = [FleetAgent(url=u).private_address for u in ("http://agent-legal:8000", "http://127.0.0.1:9", "http://10.0.0.4",
                                                           "http://localhost:8000")]
    public = [FleetAgent(url=u).private_address for u in ("https://agent-legal", "http://8.8.8.8", "http://agent.example.com")]
    assert all(private) and not any(public)


def test_on_a_private_network_role_checks_fail_closed_whatever_the_browser_sends():
    """No Easy Auth means no signed role claims: with the claims header switched off (""), a console role can't be
    satisfied by a forged header, even by a signed-in user."""
    principal = base64.b64encode(json.dumps({"claims": [{"typ": "roles", "val": "Console.Read"}]}).encode()).decode()
    app = agent_app("orders", console_role="Console.Read", **VPS_AGENT).app
    forged = {"x-forwarded-email": "ben@contoso.example", "x-ms-client-principal": principal,
              "x-agentkit-disabled-claims": principal, "": principal}
    with TestClient(app) as http:
        assert http.get("/v1/console/overview", headers={k: v for k, v in forged.items() if k}).status_code == 403
