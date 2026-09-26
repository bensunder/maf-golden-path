"""HTTP contract tests (no network, no model)."""

import json

from agentkit.hosting import create_app
from agentkit.testing import ScriptedChatClient, reply
from fastapi.testclient import TestClient

from order_status_agent import AGENT_NAME, create_agent
from order_status_agent.app import channels


def test_probes_and_chat(settings):
    client = ScriptedChatClient(script=[reply("hello there")])
    app = create_app(lambda s: create_agent(s, client=client), settings=settings, configure_telemetry=False)
    with TestClient(app) as http:
        assert http.get("/healthz").json()["status"] == "ok"
        assert http.get("/readyz").json()["agent"] == AGENT_NAME
        body = http.post("/v1/chat", json={"message": "hi"}).json()
        assert body["reply"] == "hello there"
        assert body["session_id"]


def test_web_chat_and_agui(settings):
    client = ScriptedChatClient(script=[reply("hello there")])
    app = create_app(lambda s: create_agent(s, client=client), settings=settings, configure_telemetry=False,
                     channels=channels())
    with TestClient(app) as http:
        assert "<agentkit-chat" in http.get("/chat").text
        run = {"threadId": "t1", "runId": "r1", "messages": [{"id": "m1", "role": "user", "content": "hi"}],
               "state": {}, "tools": [], "context": [], "forwardedProps": {}}
        stream = http.post("/v1/agui", json=run).text
    events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]
    assert "".join(e.get("delta", "") for e in events) == "hello there"
    assert events[-1]["type"] == "RUN_FINISHED"


def test_console_shows_this_agent(settings):
    app = create_app(lambda s: create_agent(s, client=ScriptedChatClient()), settings=settings, configure_telemetry=False,
                     channels=channels())
    user = {"x-ms-client-principal-name": "dev@contoso.example"}
    with TestClient(app) as http:
        assert http.get("/console").status_code == 200
        overview = http.get("/v1/console/overview", headers=user).json()
        evals = http.get("/v1/console/evals", headers=user).json()
    assert overview["agent"]["name"] == AGENT_NAME
    assert {c["id"] for c in overview["security"] if c["status"] == "on"} >= {"pii", "token_budget"}
    assert evals["cases"], "the console lists evals/cases.yaml"
