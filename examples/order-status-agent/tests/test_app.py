"""HTTP contract tests (no network, no model)."""

from agentkit.hosting import create_app
from agentkit.testing import ScriptedChatClient, reply
from fastapi.testclient import TestClient

from order_status_agent import AGENT_NAME, create_agent


def test_probes_and_chat(settings):
    client = ScriptedChatClient(script=[reply("hello there")])
    app = create_app(lambda s: create_agent(s, client=client), settings=settings, configure_telemetry=False)
    with TestClient(app) as http:
        assert http.get("/healthz").json()["status"] == "ok"
        assert http.get("/readyz").json()["agent"] == AGENT_NAME
        body = http.post("/v1/chat", json={"message": "hi"}).json()
        assert body["reply"] == "hello there"
        assert body["session_id"]
