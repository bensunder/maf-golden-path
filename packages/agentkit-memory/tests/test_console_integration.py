"""The console sees memory: the overview's ``memory`` block and the Security page's control."""

import pytest
from fastapi.testclient import TestClient

from agentkit.channels import Console
from agentkit.hosting import AgentKitSettings, build_agent, create_app
from agentkit.memory import InMemoryMemoryStore, MemoryChannel, MemoryService, UserMemoryProvider
from agentkit.testing import ScriptedChatClient

USER = {"x-ms-client-principal-name": "sam@contoso.example"}


def overview(memory: MemoryService | None, *, with_channel: bool = True) -> dict:
    settings = AgentKitSettings(environment="test", guardrail_mode="heuristic", service_name="payments",
                                require_user=True, _env_file=None)
    providers = [UserMemoryProvider(memory)] if memory else []
    channels = [Console(title="Payments")]
    if memory and with_channel:
        channels.append(MemoryChannel(memory))
    app = create_app(lambda s: build_agent(name="payments", instructions="x", settings=s,
                                           client=ScriptedChatClient(), context_providers=providers),
                     settings=settings, configure_telemetry=False, channels=channels)
    with TestClient(app) as http:
        r = http.get("/v1/console/overview", headers=USER)
        assert r.status_code == 200
        return r.json()


def control(body: dict, cid: str) -> dict | None:
    return next((c for c in body["security"] if c["id"] == cid), None)


def test_agents_without_memory_show_none_and_no_control():
    body = overview(None)
    assert body["memory"] is None and control(body, "long_term_memory") is None


def test_memory_is_described_with_its_api_path():
    memory = MemoryService(InMemoryMemoryStore(), salt="console-test-salt-0123456789")
    body = overview(memory)
    assert body["memory"]["profile"] == "user" and body["memory"]["api"] == "/v1/memory"
    assert body["memory"]["retention_days"] == 180 and body["memory"]["max_items"] == 50
    assert {"id": "memory", "name": "Memory API", "path": "/v1/memory"} in body["channels"]


@pytest.mark.parametrize("config,status,gap", [
    ({"store": "memory", "salt_configured": True}, "partial", "loses memory on restart"),
    ({"store": "redis", "salt_configured": False}, "partial", "AGENTKIT_MEMORY_SALT"),
    ({"store": "redis", "salt_configured": True}, "on", None),
])
def test_security_control_reports_gaps(config, status, gap):
    memory = MemoryService(InMemoryMemoryStore(), salt="console-test-salt-0123456789")
    memory.config.update(config)
    c = control(overview(memory), "long_term_memory")
    assert c["status"] == status and "refused" in c["detail"]
    assert (gap in c["detail"]) if gap else ("Gaps" not in c["detail"])


def test_memory_without_the_api_channel_has_no_api_path():
    memory = MemoryService(InMemoryMemoryStore(), salt="console-test-salt-0123456789")
    assert overview(memory, with_channel=False)["memory"]["api"] is None
