"""Human approvals end to end through the HTTP host."""

import base64
import json

import pytest
from agent_framework import tool
from fastapi.testclient import TestClient

from agentkit.hosting import (
    AgentKitSettings,
    RedisSessionStore,
    approve_if,
    build_agent,
    create_app,
    roles_from_principal,
)
from agentkit.testing import ScriptedChatClient, ToolCall, Turn, reply, tool_call

LOCAL = dict(environment="local", guardrail_mode="heuristic", _env_file=None)
BEN = {"x-ms-client-principal-name": "ben"}
EVE = {"x-ms-client-principal-name": "eve"}

REFUNDS: list[tuple[str, float]] = []
EMAILS: list[str] = []


@tool(approval_mode="always_require")
def issue_refund(order_id: str, amount: float) -> str:
    """Refund part of an order."""
    REFUNDS.append((order_id, amount))
    return f"Refunded ${amount:.2f} on {order_id}"


@tool(approval_mode="always_require")
def email_customer(order_id: str, text: str) -> str:
    """Email the customer."""
    EMAILS.append(order_id)
    return "sent"


def principal(name: str, *roles: str) -> dict[str, str]:
    claims = [{"typ": "roles", "val": r} for r in roles] + [{"typ": "name", "val": name}]
    encoded = base64.b64encode(json.dumps({"auth_typ": "aad", "claims": claims}).encode()).decode()
    return {"x-ms-client-principal-name": name, "x-ms-client-principal": encoded}


def make_app(client, *, store=None, with_rules=True, **settings_overrides):
    settings = AgentKitSettings(**{**LOCAL, **settings_overrides})
    rules = [approve_if("issue_refund", lambda a: a["amount"] <= 50)] if with_rules else []
    app = create_app(
        lambda s: build_agent(name="orders", instructions="x", tools=[issue_refund, email_customer],
                              settings=s, client=client, approval_rules=rules),
        settings=settings, session_store=store, configure_telemetry=False,
    )
    return app


@pytest.fixture(autouse=True)
def _reset():
    REFUNDS.clear()
    EMAILS.clear()


# --- confirmation mode (no approver role): the requester confirms -------------------------------------


def test_small_refund_is_auto_approved():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A1", amount=20), reply("Done: $20 refunded.")])
    with TestClient(make_app(client)) as http:
        body = http.post("/v1/chat", json={"message": "refund $20 on A1"}, headers=BEN).json()
    assert body["status"] == "completed" and body["approvals"] == []
    assert REFUNDS == [("A1", 20.0)]


def test_large_refund_waits_then_runs_after_confirmation():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A1", amount=80), reply("Refunded $80.")])
    with TestClient(make_app(client)) as http:
        first = http.post("/v1/chat", json={"message": "refund $80 on A1"}, headers=BEN).json()
        assert first["status"] == "approval_required"
        (approval,) = first["approvals"]
        assert approval["tool"] == "issue_refund" and approval["arguments"] == {"order_id": "A1", "amount": 80}
        assert REFUNDS == []  # nothing happened yet
        sid = first["session_id"]

        # the conversation is paused: new messages are refused until someone decides
        blocked = http.post("/v1/chat", json={"message": "hello?", "session_id": sid}, headers=BEN)
        assert blocked.status_code == 409 and "pending" in json.dumps(blocked.json())

        # someone else can't confirm another user's action
        assert http.post(f"/v1/sessions/{sid}/approvals", headers=EVE,
                         json={"decisions": [{"id": approval["id"], "approved": True}]}).status_code == 403

        done = http.post(f"/v1/sessions/{sid}/approvals", headers=BEN,
                         json={"decisions": [{"id": approval["id"], "approved": True, "comment": "customer called"}]}).json()
        assert done["status"] == "completed" and done["reply"] == "Refunded $80."
        assert REFUNDS == [("A1", 80.0)]

        record = http.app.state.session_store._data[sid]
        (entry,) = record.meta["approval_log"]
        assert entry["approved"] is True and entry["decided_by"] == "ben" and entry["comment"] == "customer called"
        assert record.meta["approvals"] == []

        # conversation continues normally afterwards
        client.enqueue(reply("Anything else?"))
        assert http.post("/v1/chat", json={"message": "thanks", "session_id": sid}, headers=BEN).status_code == 200


def test_rejection_never_runs_the_tool_and_model_is_told():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A1", amount=80), reply("Understood, no refund.")])
    with TestClient(make_app(client)) as http:
        first = http.post("/v1/chat", json={"message": "refund"}, headers=BEN).json()
        done = http.post(f"/v1/sessions/{first['session_id']}/approvals", headers=BEN,
                         json={"decisions": [{"id": first["approvals"][0]["id"], "approved": False}]}).json()
    assert done["status"] == "completed" and REFUNDS == []
    assert "rejected" in str(list(client.tool_results().values())[-1])


def test_ids_are_validated_before_reaching_maf():
    client = ScriptedChatClient(
        script=[Turn(tool_calls=(ToolCall("issue_refund", {"order_id": "A1", "amount": 80}),
                                 ToolCall("email_customer", {"order_id": "A1", "text": "hi"}))),
                reply("Both done.")]
    )
    with TestClient(make_app(client, with_rules=False)) as http:  # plain MAF: both surface at once
        first = http.post("/v1/chat", json={"message": "refund and email"}, headers=BEN).json()
        sid, ids = first["session_id"], [a["id"] for a in first["approvals"]]
        assert len(ids) == 2
        forged = http.post(f"/v1/sessions/{sid}/approvals", headers=BEN,
                           json={"decisions": [{"id": "af-call-forged", "approved": True}]})
        assert forged.status_code == 400
        partial = http.post(f"/v1/sessions/{sid}/approvals", headers=BEN,
                            json={"decisions": [{"id": ids[0], "approved": True}]})
        assert partial.status_code == 409 and "missing" in partial.json()["detail"]
        done = http.post(f"/v1/sessions/{sid}/approvals", headers=BEN,
                         json={"decisions": [{"id": ids[0], "approved": True}, {"id": ids[1], "approved": False}]}).json()
    assert done["status"] == "completed"
    assert REFUNDS == [("A1", 80.0)] and EMAILS == []
    assert len(client.calls) == 2


def test_approvals_chain_one_at_a_time_with_rules():
    """With auto-approval rules, MAF queues approvals and surfaces them one by one."""
    client = ScriptedChatClient(
        script=[Turn(tool_calls=(ToolCall("issue_refund", {"order_id": "A1", "amount": 80}),
                                 ToolCall("email_customer", {"order_id": "A1", "text": "hi"}))),
                reply("Refunded; email not sent.")]
    )
    with TestClient(make_app(client)) as http:
        first = http.post("/v1/chat", json={"message": "refund and email"}, headers=BEN).json()
        sid = first["session_id"]
        assert [a["tool"] for a in first["approvals"]] == ["issue_refund"]
        second = http.post(f"/v1/sessions/{sid}/approvals", headers=BEN,
                           json={"decisions": [{"id": first["approvals"][0]["id"], "approved": True}]}).json()
        assert second["status"] == "approval_required" and [a["tool"] for a in second["approvals"]] == ["email_customer"]
        done = http.post(f"/v1/sessions/{sid}/approvals", headers=BEN,
                         json={"decisions": [{"id": second["approvals"][0]["id"], "approved": False}]}).json()
        log = http.app.state.session_store._data[sid].meta["approval_log"]
    assert done["status"] == "completed"
    assert REFUNDS == [("A1", 80.0)] and EMAILS == []
    assert [(e["tool"], e["approved"]) for e in log] == [("issue_refund", True), ("email_customer", False)]


def test_no_pending_approval_is_409():
    client = ScriptedChatClient(script=[reply("hi")])
    with TestClient(make_app(client)) as http:
        sid = http.post("/v1/chat", json={"message": "hi"}, headers=BEN).json()["session_id"]
        r = http.post(f"/v1/sessions/{sid}/approvals", headers=BEN, json={"decisions": [{"id": "x", "approved": True}]})
    assert r.status_code == 409


# --- separation of duties (approver role) --------------------------------------------------------------


def test_separation_of_duties():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A2", amount=500), reply("Refunded $500.")])
    with TestClient(make_app(client, approver_role="Refunds.Approve")) as http:
        first = http.post("/v1/chat", json={"message": "refund $500"}, headers=BEN).json()
        sid, aid = first["session_id"], first["approvals"][0]["id"]
        decision = {"decisions": [{"id": aid, "approved": True}]}

        # requester, even with the role, can't approve their own request
        assert http.post(f"/v1/sessions/{sid}/approvals", headers=principal("ben", "Refunds.Approve"), json=decision).status_code == 403
        # someone without the role can't either
        assert http.post(f"/v1/sessions/{sid}/approvals", headers=principal("eve"), json=decision).status_code == 403
        # an approver can see and decide it
        listed = http.get(f"/v1/sessions/{sid}/approvals", headers=principal("maria", "Refunds.Approve")).json()
        assert [a["id"] for a in listed] == [aid]
        assert http.get(f"/v1/sessions/{sid}/approvals", headers=principal("eve")).status_code == 403
        done = http.post(f"/v1/sessions/{sid}/approvals", headers=principal("maria", "Refunds.Approve"), json=decision).json()
        assert done["status"] == "completed" and REFUNDS == [("A2", 500.0)]
        entry = http.app.state.session_store._data[sid].meta["approval_log"][0]
        assert entry["decided_by"] == "maria" and entry["requested_by"] == "ben"


def test_roles_from_principal_parsing():
    assert roles_from_principal(principal("x", "A", "B")["x-ms-client-principal"]) == {"A", "B"}
    assert roles_from_principal("not-base64!!") == set()
    assert roles_from_principal(None) == set()


# --- streaming --------------------------------------------------------------------------------------------


def test_streaming_emits_approval_required():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A1", amount=80)])
    with TestClient(make_app(client)) as http:
        with http.stream("POST", "/v1/chat/stream", json={"message": "refund"}, headers=BEN) as response:
            payload = "".join(response.iter_text())
    assert "event: approval_required" in payload
    done = json.loads(payload.split("event: done\ndata: ")[1].strip())
    assert done["status"] == "approval_required"


# --- scale-out: request on one replica, decision on another -------------------------------------------------


def test_approval_crosses_replicas_via_redis(redis_url):
    import time

    prefix = f"approvals{time.time_ns()}:"
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A9", amount=90), reply("Refunded $90.")])
    replica_a = make_app(client, store=RedisSessionStore.from_url(redis_url, prefix=prefix))
    replica_b = make_app(client, store=RedisSessionStore.from_url(redis_url, prefix=prefix))
    with TestClient(replica_a) as a, TestClient(replica_b) as b:
        first = a.post("/v1/chat", json={"message": "refund $90 on A9"}, headers=BEN).json()
        assert first["status"] == "approval_required"
        done = b.post(f"/v1/sessions/{first['session_id']}/approvals", headers=BEN,
                      json={"decisions": [{"id": first["approvals"][0]["id"], "approved": True}]}).json()
        assert done["reply"] == "Refunded $90."
        client.enqueue(reply("Your refund went through."))
        follow = a.post("/v1/chat", json={"message": "did it work?", "session_id": first["session_id"]}, headers=BEN).json()
    assert REFUNDS == [("A9", 90.0)]
    assert follow["status"] == "completed"
    history = [m.text for m in client.calls[-1].messages if m.role == "user" and m.text]
    assert history[0] == "refund $90 on A9" and history[-1] == "did it work?"


# --- busy session ------------------------------------------------------------------------------------------------


def test_busy_session_returns_409_when_another_replica_holds_it(redis_url):
    import asyncio
    import time

    prefix = f"busy{time.time_ns()}:"
    client = ScriptedChatClient(script=[reply("hi")])
    store = RedisSessionStore.from_url(redis_url, prefix=prefix, lock_wait_seconds=0.2)
    with TestClient(make_app(client, store=store)) as http:
        sid = http.post("/v1/chat", json={"message": "hi"}, headers=BEN).json()["session_id"]

        async def hold_from_other_replica():
            other = RedisSessionStore.from_url(redis_url, prefix=prefix)
            ctx = other.lock(sid)
            await ctx.__aenter__()
            await other._r.aclose()  # connection gone, lock key stays until its TTL: like a busy replica

        asyncio.run(hold_from_other_replica())
        r = http.post("/v1/chat", json={"message": "again", "session_id": sid}, headers=BEN)
    assert r.status_code == 409 and "busy" in r.json()["detail"]


async def test_approval_rules_work_without_a_session():
    """MAF's ToolApprovalMiddleware needs a session; build_agent supplies one so plain runs work."""
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A1", amount=10), reply("ok")])
    agent = build_agent(name="w", instructions="x", tools=[issue_refund], settings=AgentKitSettings(**LOCAL),
                        client=client, approval_rules=[approve_if("issue_refund", lambda a: a["amount"] <= 50)])
    assert (await agent.run("refund $10")).text == "ok"
    assert REFUNDS == [("A1", 10.0)]


def test_broken_rule_never_auto_approves():
    from agent_framework import Content

    rule = approve_if("issue_refund", lambda a: a["missing_key"] < 5)
    call = Content.from_function_call(call_id="c", name="issue_refund", arguments={"amount": 1})
    assert rule(call) is False
