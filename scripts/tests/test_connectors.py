"""deploy/vps/platform: the Connectors catalog (MCP servers agents may use) and the connector gateway."""

import asyncio
import base64
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

PLATFORM = Path(__file__).resolve().parents[2] / "deploy" / "vps" / "platform"
KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
ADMIN_W = {"x-forwarded-email": "ben@contoso.example", "origin": "https://agent.203-0-113-7.sslip.io"}
OTHER = {"x-forwarded-email": "someone@contoso.example"}

DEMO = '''
import sys
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
mcp = FastMCP("demo-crm", host="127.0.0.1", port=int(sys.argv[1]), stateless_http=True)
seen = []
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_contact(email: str) -> str:
    """Find a CRM contact by email."""
    return f"Contact {email}: Dana Lee, Acme Corp"
@mcp.tool()
def create_note(contact_email: str, text: str) -> str:
    """Add a note to a contact."""
    return f"Note added to {contact_email}"
@mcp.tool()
def delete_contact(email: str) -> str:
    """Delete a contact."""
    return f"Deleted {email}"
mcp.run(transport="streamable-http")
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def demo_server(tmp_path_factory):
    """A real MCP server (streamable HTTP) standing in for a vendor; it only accepts a bearer token."""
    folder = tmp_path_factory.mktemp("demo")
    script = folder / "demo.py"
    script.write_text(DEMO + "")
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(script), str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/mcp", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()


@pytest.fixture
def platform(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_CONNECTOR_ALLOW_PRIVATE", "1")  # the demo server is local http
    for name in ("connectors", "platform_server"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("platform_server", PLATFORM / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = tmp_path / "kit"
    (home / "deploy" / "vps").mkdir(parents=True)
    monkeypatch.setattr(mod, "HOME", home)
    monkeypatch.setattr(mod, "VPS", home / "deploy" / "vps")
    monkeypatch.setattr(mod, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(mod, "ADMINS", {"ben@contoso.example"})
    monkeypatch.setattr(mod, "PUBLIC_HOST", "agent.203-0-113-7.sslip.io")
    store = mod.Store(home / "deploy" / "vps" / "connectors.json", mod.Vault(KEY))
    monkeypatch.setattr(mod, "STORE", store)
    (tmp_path / "agents").mkdir()
    return mod


def registry(mod, agents, sample=None):
    (mod.VPS / "agents.yaml").write_text(yaml.safe_dump({"agents": agents, "sample": sample}))


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, job, argv, cwd, timeout=600):
        self.calls.append(argv)


def test_admins_add_a_connector_whose_credential_never_comes_back(platform, demo_server):
    registry(platform, [])
    with TestClient(platform.create_app(platform.Builder(runner=Recorder()), trusted_peer=None)) as http:
        tested = http.post("/v1/platform/connectors/test", headers=ADMIN_W,
                           json={"url": demo_server, "auth": "bearer", "secret": "vendor-secret-123"}).json()
        names = {t["name"]: t["read_only"] for t in tested["tools"]}
        assert names == {"find_contact": True, "create_note": False, "delete_contact": False}
        body = {"name": "crm", "title": "Demo CRM", "url": demo_server, "auth": "bearer", "secret": "vendor-secret-123",
                "tools": tested["tools"], "allowed": ["find_contact", "create_note"], "approval": ["create_note"]}
        assert http.post("/v1/platform/connectors", headers={**OTHER, "origin": ADMIN_W["origin"]}, json=body).status_code == 403
        created = http.post("/v1/platform/connectors", headers=ADMIN_W, json=body)
        assert created.status_code == 201
        listed = http.get("/v1/platform/connectors", headers=OTHER).json()
    view = listed["connectors"][0]
    assert view["has_secret"] and view["allowed"] == ["create_note", "find_contact"] and view["approval"] == ["create_note"]
    assert "vendor-secret-123" not in json.dumps(listed) and "secret" not in view
    stored = (platform.VPS / "connectors.json").read_text()
    assert "vendor-secret-123" not in stored  # sealed at rest
    assert oct((platform.VPS / "connectors.json").stat().st_mode & 0o777) == "0o600"
    assert platform.STORE.credential_headers(platform.STORE.get("crm")) == {"authorization": "Bearer vendor-secret-123"}


@pytest.mark.parametrize("body", [
    {"name": "Bad Name", "url": "https://mcp.linear.app/mcp", "auth": "none"},
    {"name": "crm", "url": "https://mcp.linear.app/mcp", "auth": "header", "header": "Cookie", "secret": "x"},
    {"name": "crm", "url": "https://mcp.linear.app/mcp", "auth": "bearer"},  # no secret
    {"name": "crm", "url": "https://mcp.linear.app/mcp", "auth": "magic"},
    {"name": "crm", "url": "https://mcp.linear.app/mcp", "auth": "none", "allowed": ["x; rm"]},
])
def test_connector_input_is_checked(platform, body):
    registry(platform, [])
    with TestClient(platform.create_app(platform.Builder(runner=Recorder()), trusted_peer=None)) as http:
        assert http.post("/v1/platform/connectors", headers=ADMIN_W, json=body).status_code == 422


def test_connectors_must_be_public_https_services(monkeypatch):
    monkeypatch.delenv("PLATFORM_CONNECTOR_ALLOW_PRIVATE", raising=False)
    sys.modules.pop("connectors", None)
    sys.path.insert(0, str(PLATFORM))
    import connectors
    connectors = importlib.reload(connectors)
    for url in ("http://mcp.linear.app/mcp", "https://127.0.0.1/mcp", "https://10.0.0.5/mcp", "https://169.254.169.254/latest",
                "https://user:pw@mcp.linear.app/mcp", "https://localhost/mcp"):
        with pytest.raises(connectors.ConnectorError):
            asyncio.run(connectors.check_url(url))


def test_gateway_adds_the_credential_passes_allowed_tools_and_refuses_the_rest(platform, demo_server):
    token = "t" * 40
    registry(platform, [{"name": "legal", "internal": True, "connectors": ["crm"], "connector_token": token},
                        {"name": "hr", "internal": True, "connectors": [], "connector_token": "h" * 40}])
    platform.STORE.save({"crm": {"title": "Demo CRM", "url": demo_server, "auth": "bearer",
                                 "secret": platform.STORE.vault.seal("vendor-secret-123"),
                                 "tools": [], "allowed": ["find_contact", "create_note"], "approval": ["create_note"]}})
    seen = []

    async def vendor(request: httpx.Request) -> httpx.Response:  # records what reached the vendor
        seen.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
                              headers={"mcp-session-id": "s1", "x-internal": "no"})

    gateway = platform.create_gateway(platform.STORE, platform.assignments, platform.ACTIVITY,
                                      client=httpx.AsyncClient(transport=httpx.MockTransport(vendor)))
    call = lambda tool, **h: {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": tool, "arguments": {}}}
    policy = platform.policy_of(platform.STORE.get("crm"))
    agent = {"authorization": f"Bearer {token}", "x-agentkit-connector-policy": policy}
    with TestClient(gateway) as http:
        ok = http.post("/mcp/crm", headers={**agent, "cookie": "x=1", "mcp-session-id": "s1"}, json=call("find_contact"))
        refused = http.post("/mcp/crm", headers=agent, json=call("delete_contact"))
        batch = http.post("/mcp/crm", headers=agent, json=[call("find_contact"), call("delete_contact")])
        stranger = http.post("/mcp/crm", headers={"authorization": "Bearer " + "x" * 40}, json=call("find_contact"))
        unassigned = http.post("/mcp/crm", headers={"authorization": "Bearer " + "h" * 40}, json=call("find_contact"))
        no_token = http.post("/mcp/crm", json=call("find_contact"))
    assert ok.status_code == 200 and ok.headers["mcp-session-id"] == "s1" and "x-internal" not in ok.headers
    first = seen[0]
    assert first.headers["authorization"] == "Bearer vendor-secret-123"  # the vendor credential, not the agent's token
    assert "cookie" not in first.headers and first.headers["mcp-session-id"] == "s1"
    assert refused.json()["error"]["message"].startswith("tool 'delete_contact' isn't allowed")
    assert isinstance(batch.json(), list) and len(seen) == 1  # a batch with one refused call never reaches the vendor
    assert (stranger.status_code, unassigned.status_code, no_token.status_code) == (401, 403, 401)
    tools = [(a["tool"], a["allowed"]) for a in platform.ACTIVITY.for_connector("crm")]
    assert ("delete_contact", False) in tools and ("find_contact", True) in tools


def test_assigning_connectors_restarts_the_agent_and_refreshes_users_of_a_changed_connector(platform, demo_server):
    registry(platform, [{"name": "legal", "internal": True, "connectors": ["crm"], "connector_token": "t" * 40}])
    platform.STORE.save({"crm": {"title": "Demo CRM", "url": demo_server, "auth": "none", "tools": [
        {"name": "find_contact", "description": "", "read_only": True}, {"name": "create_note", "description": "", "read_only": False}],
        "allowed": ["find_contact"], "approval": []}})
    recorder = Recorder()
    builder = platform.Builder(runner=recorder)

    async def ready(job, base=None):
        return None

    builder.wait_ready = ready
    with TestClient(platform.create_app(builder, trusted_peer=None)) as http:
        job = http.put("/v1/platform/agents/sample/connectors", headers=ADMIN_W, json={"connectors": ["crm"]})
        assert job.status_code == 202
        for _ in range(100):
            if http.get(f"/v1/platform/jobs/{job.json()['id']}", headers=ADMIN_W).json()["state"] in ("ready", "failed"):
                break
            time.sleep(0.02)
        assert http.put("/v1/platform/agents/sample/connectors", headers=ADMIN_W, json={"connectors": ["nope"]}).status_code == 422
        changed = http.patch("/v1/platform/connectors/crm", headers=ADMIN_W,
                             json={"allowed": ["find_contact", "create_note"], "approval": ["create_note"]}).json()
        assert changed["job"] and changed["used_by"] == ["legal"]
        for _ in range(100):
            if http.get(f"/v1/platform/jobs/{changed['job']['id']}", headers=ADMIN_W).json()["state"] in ("ready", "failed"):
                break
            time.sleep(0.02)
        in_use = http.delete("/v1/platform/connectors/crm", headers={**ADMIN_W, "x-agentkit-confirm": "crm"})
    assert [sys.executable if c[0] == sys.executable else c[0] for c in recorder.calls][0] == sys.executable
    assert recorder.calls[0][1:] == ["agentctl.py", "connect", "sample", "crm"]
    assert recorder.calls[1] == ["docker", "compose", "up", "-d", "agent"]
    assert recorder.calls[2][1:] == ["agentctl.py", "render"] and recorder.calls[3] == ["docker", "compose", "up", "-d", "agent-legal"]
    assert in_use.status_code == 409  # still used by an agent



# ------------------------------------------------------------------ the gateway only speaks MCP, and only as checked
def _gateway(platform, vendor_handler, *, allowed=("find_contact",), approval=()):
    token = "t" * 40
    registry(platform, [{"name": "legal", "internal": True, "connectors": ["crm"], "connector_token": token}])
    platform.STORE.save({"crm": {"title": "CRM", "url": "https://vendor.example.com/mcp", "auth": "header", "header": "X-API-Key",
                                 "secret": platform.STORE.vault.seal("SECRET"), "tools": [],
                                 "allowed": list(allowed), "approval": list(approval)}})
    gateway = platform.create_gateway(platform.STORE, platform.assignments, platform.ACTIVITY,
                                      client=httpx.AsyncClient(transport=httpx.MockTransport(vendor_handler)))
    headers = {"authorization": f"Bearer {token}", "x-agentkit-connector-policy": platform.policy_of(platform.STORE.get("crm"))}
    return TestClient(gateway), headers


def test_nothing_but_the_stored_url_reaches_the_vendor(platform):
    seen = []

    async def vendor(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    http, h = _gateway(platform, vendor)
    with http:
        for path in ("/mcp/crm/%2e%2e/api/v1/admin/users?x=1", "/mcp/crm/../api", "/mcp/crm/extra", "/mcp/crm?x=1"):
            http.get(path, headers=h)
        http.post("/mcp/crm?admin=1", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert set(seen) <= {"https://vendor.example.com/mcp"} and seen  # never another path, never a query


@pytest.mark.parametrize("raw", [
    b'{"jsonrpc":"2.0","id":1,"method":"resources/read","params":{"uri":"file:///etc/passwd"}}',
    b'{"jsonrpc":"2.0","id":1,"method":"prompts/get","params":{"name":"x"}}',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"find_contact","Name":"delete_contact"}}',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"find_contact"},"params":{"name":"delete_contact"}}',
    b'{"jsonrpc":"2.0","id":1,"Method":"tools/call","method":"ping"}',
    b'not json',
    b'[]',
])
def test_messages_the_vendor_could_read_differently_are_refused(platform, raw):
    seen = []

    async def vendor(request):
        seen.append(request)
        return httpx.Response(200, json={})

    http, h = _gateway(platform, vendor)
    with http:
        response = http.post("/mcp/crm", headers={**h, "content-type": "application/json"}, content=raw)
    assert response.status_code == 400 and not seen


def test_what_reaches_the_vendor_is_exactly_what_was_checked_and_listings_hide_other_tools(platform):
    seen = []
    listing = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "find_contact"}, {"name": "delete_contact"}]}}

    async def vendor(request):
        seen.append(request.content)
        body = json.loads(request.content)
        if body.get("method") == "tools/list":
            if request.headers.get("accept") == "text/event-stream":
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=f"event: message\ndata: {json.dumps(listing)}\n\n".encode())
            return httpx.Response(200, json=listing)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    http, h = _gateway(platform, vendor)
    with http:
        http.post("/mcp/crm", headers={**h, "content-type": "application/json"},
                  content=b'{"jsonrpc": "2.0",   "id": 1, "method": "tools/call", "params": {"name": "find_contact", "arguments": {"email": "a@b.c"}}}')
        as_json = http.post("/mcp/crm", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
        as_sse = http.post("/mcp/crm", headers={**h, "accept": "text/event-stream"}, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).text
    assert seen[0] == json.dumps(json.loads(seen[0]), separators=(",", ":")).encode()  # re-encoded, not the raw bytes
    assert [t["name"] for t in as_json["result"]["tools"]] == ["find_contact"]
    assert "delete_contact" not in as_sse and "find_contact" in as_sse


def test_an_agent_started_with_old_tool_rules_cant_call_tools(platform):
    seen = []

    async def vendor(request):
        seen.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    http, h = _gateway(platform, vendor, allowed=("find_contact", "create_note"), approval=("create_note",))
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "create_note", "arguments": {}}}
    with http:
        stale = http.post("/mcp/crm", headers={**h, "x-agentkit-connector-policy": "0" * 16}, json=call).json()
        missing = http.post("/mcp/crm", headers={"authorization": h["authorization"]}, json=call).json()
        ping = http.post("/mcp/crm", headers={"authorization": h["authorization"]}, json={"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert "tool rules changed" in stale["error"]["message"] and "tool rules changed" in missing["error"]["message"]
    assert ping.status_code == 200 and len(seen) == 1  # only the ping reached the vendor


def test_oversized_requests_are_refused_before_they_are_read(platform):
    http, h = _gateway(platform, lambda r: httpx.Response(200))
    with http:
        big = http.post("/mcp/crm", headers={**h, "content-type": "application/json"}, content=b"[" + b"1," * 600_000 + b"1]")
    assert big.status_code == 413


def test_a_broken_or_changed_key_stops_connectors_not_the_platform(platform):
    broken = platform.Vault("not-a-key")
    assert not broken.ready and "isn't a valid key" in broken.error
    sealed = platform.STORE.vault.seal("SECRET")
    other = platform.Vault(base64.urlsafe_b64encode(b"o" * 32).decode())
    with pytest.raises(platform.ConnectorError, match="can't be decrypted"):
        other.open(sealed)


def test_only_admins_see_connector_urls_and_changes_wait_for_a_running_job(platform, demo_server):
    registry(platform, [{"name": "legal", "internal": True, "connectors": ["crm"], "connector_token": "t" * 40}])
    platform.STORE.save({"crm": {"title": "CRM", "url": demo_server + "?key=abc", "auth": "none", "tools": [],
                                 "allowed": ["find_contact"], "approval": []}})
    builder = platform.Builder(runner=Recorder())
    busy_job = platform.Job("x", "x", "ben@contoso.example")
    builder.jobs[busy_job.id] = busy_job  # a creation in progress
    with TestClient(platform.create_app(builder, trusted_peer=None)) as http:
        assert http.get("/v1/platform/connectors", headers=OTHER).json()["connectors"][0]["url"] is None
        assert "key=abc" in http.get("/v1/platform/connectors", headers=ADMIN_W).json()["connectors"][0]["url"]
        refused = http.patch("/v1/platform/connectors/crm", headers=ADMIN_W, json={"allowed": ["find_contact", "create_note"]})
    assert refused.status_code == 409
    assert platform.STORE.get("crm")["allowed"] == ["find_contact"]  # nothing saved: rules and agents stay in step


def test_agentctl_and_the_gateway_agree_on_the_rules_fingerprint(platform):
    spec = importlib.util.spec_from_file_location("agentctl_mod", PLATFORM.parent / "agentctl.py")
    agentctl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agentctl)
    entry = {"allowed": ["b", "a"], "approval": ["b"]}
    assert agentctl.policy_of(entry) == platform.policy_of(entry) == platform.policy_of({"allowed": ["a", "b"], "approval": ["b"]})
