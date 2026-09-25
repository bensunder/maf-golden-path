"""Teams channel end to end: real activities in through /api/messages, replies captured by a fake
Bot Connector, approvals clicked on the Adaptive Cards the agent posts."""

import pytest
from agent_framework import tool

from agentkit.channels import StaticApprovers, TeamsChannel, TeamsSettings, teams_session_id
from agentkit.channels.cards import VERB_APPROVE
from agentkit.channels.testing import TeamsTestClient, TeamsTestUser, teams_test_settings
from agentkit.hosting import AgentKitSettings, InMemorySessionStore, approve_if, build_agent, create_app
from agentkit.testing import ScriptedChatClient, reply, tool_call

REFUNDS: list[tuple[str, float]] = []
ALICE, BOB, EVE = TeamsTestUser("alice"), TeamsTestUser("bob"), TeamsTestUser("eve")
APPROVERS_CHANNEL = "19:approvers@thread.tacv2"


@tool(approval_mode="always_require")
def issue_refund(order_id: str, amount: float) -> str:
    """Refund part of an order."""
    REFUNDS.append((order_id, amount))
    return f"Refunded ${amount:.2f} on {order_id}"


@pytest.fixture(autouse=True)
def _reset():
    REFUNDS.clear()


def make_app(client, *, approver_role=None, teams=None, approvers=None, store=None):
    settings = AgentKitSettings(environment="test", guardrail_mode="heuristic", approver_role=approver_role,
                                _env_file=None)
    channel = TeamsChannel(teams or teams_test_settings(), approvers=approvers)
    app = create_app(
        lambda s: build_agent(name="orders", instructions="x", tools=[issue_refund], settings=s, client=client,
                              approval_rules=[approve_if("issue_refund", lambda a: a["amount"] <= 50)]),
        settings=settings, session_store=store or InMemorySessionStore(), configure_telemetry=False,
        channels=[channel],
    )
    return app, channel


async def test_message_gets_a_reply_in_the_same_conversation():
    app, _ = make_app(ScriptedChatClient(script=[reply("Order A1 has shipped.")]))
    async with TeamsTestClient(app) as teams:
        response = await teams.send("where is A1?", user=ALICE)
        assert response.status_code == 202  # acknowledged at once; the reply is proactive
        assert teams.texts("a:alice") == ["Order A1 has shipped."]
    typing = [a for a in teams.connector.activities if a.get("type") == "typing"]
    assert typing, "a typing indicator is sent while the agent works"


async def test_conversation_continues_in_the_same_session():
    client = ScriptedChatClient(script=[reply("hi"), reply("again")])
    app, _ = make_app(client)
    async with TeamsTestClient(app) as teams:
        await teams.send("hello", user=ALICE)
        await teams.send("and again", user=ALICE)
        record = await app.state.session_store.get(teams_session_id("a:alice", ALICE.object_id))
    assert record.owner == ALICE.object_id
    assert [c.last_user_text for c in client.calls] == ["hello", "and again"]
    assert len(client.calls[1].messages) > len(client.calls[0].messages)  # history carried over


async def test_confirmation_mode_requester_confirms_on_a_card():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A1", amount=80),
                                        reply("Refunded $80.00 on A1.")])
    app, _ = make_app(client)
    async with TeamsTestClient(app) as teams:
        await teams.send("refund $80 on A1", user=ALICE)
        card = teams.last_card("a:alice")
        assert card["body"][0]["text"] == "Please confirm"
        assert [a["title"] for a in card["actions"]] == ["Approve", "Reject"]
        assert REFUNDS == []

        response = await teams.click(card, "approve", user=ALICE)
        assert response["type"] == "application/vnd.microsoft.card.adaptive"
        assert "actions" not in response["value"]  # the card is replaced: nothing left to click twice
        assert response["value"]["body"][0]["items"][0]["text"].startswith("Approved")
    assert REFUNDS == [("A1", 80.0)]
    assert teams.texts("a:alice")[-1] == "Approved by Alice. Refunded $80.00 on A1."


async def test_confirmation_mode_someone_else_cannot_confirm():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A1", amount=80), reply("done")])
    app, _ = make_app(client)
    async with TeamsTestClient(app) as teams:
        await teams.send("refund $80 on A1", user=ALICE)
        card = teams.last_card()
        response = await teams.click(card, "approve", user=EVE)
    assert response["type"] == "application/vnd.microsoft.activity.message"
    assert "only the requesting user" in response["value"]
    assert REFUNDS == []


async def test_rejection_is_reported_and_nothing_runs():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A1", amount=80),
                                        reply("The refund was not approved.")])
    app, _ = make_app(client)
    async with TeamsTestClient(app) as teams:
        await teams.send("refund $80 on A1", user=ALICE)
        await teams.click(teams.last_card(), "reject", user=ALICE, inputs={"comment": "too much"})
    assert REFUNDS == []
    assert teams.texts()[-1] == "Rejected by Alice. The refund was not approved."
    record = await app.state.session_store.get(teams_session_id("a:alice", ALICE.object_id))
    (entry,) = record.meta["approval_log"]
    assert entry["approved"] is False and entry["comment"] == "too much" and entry["channel"] == "teams"


# --- separation of duties: approvers channel ----------------------------------------------------------


def separated(client, **kw):
    return make_app(client, approver_role="Refunds.Approve",
                    teams=teams_test_settings(approvals_channel_id=APPROVERS_CHANNEL),
                    approvers=StaticApprovers([BOB.object_id, ALICE.object_id]), **kw)


async def test_separation_card_goes_to_approvers_channel_and_result_back_to_requester():
    client = ScriptedChatClient(script=[reply("noted"),  # the approvals channel registration message
                                        tool_call("issue_refund", order_id="A2", amount=129),
                                        reply("Refunded $129.00 on A2.")])
    app, _ = separated(client)
    async with TeamsTestClient(app) as teams:
        await teams.send("hello from the approvers channel", user=BOB, channel_id=APPROVERS_CHANNEL)
        await teams.send("refund $129 on A2", user=EVE)

        card = teams.last_card(APPROVERS_CHANNEL)  # posted as a new thread in the channel
        (post,) = [a for a in teams.connector.messages(APPROVERS_CHANNEL) if a.get("attachments")]
        assert not post.get("replyToId")
        assert card["body"][0]["text"] == "Approval needed"
        assert "for Eve" in card["body"][1]["text"]
        assert teams.cards("a:eve") == []
        assert "sent this to an approver" in teams.texts("a:eve")[-1]

        await teams.click(card, "approve", user=BOB)
    assert REFUNDS == [("A2", 129.0)]
    assert teams.texts("a:eve")[-1] == "Approved by Bob. Refunded $129.00 on A2."


async def test_separation_requester_cannot_approve_own_request():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A2", amount=129), reply("done")])
    app, _ = separated(client)
    async with TeamsTestClient(app) as teams:
        await teams.send("refund $129 on A2", user=ALICE)  # Alice is an approver, but it's her own request
        response = await teams.click(teams.last_card(), "approve", user=ALICE)
    assert "separation of duties" in response["value"]
    assert REFUNDS == []


async def test_separation_non_approver_is_refused():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A2", amount=129), reply("done")])
    app, _ = separated(client)
    async with TeamsTestClient(app) as teams:
        await teams.send("refund $129 on A2", user=ALICE)
        response = await teams.click(teams.last_card(), "approve", user=EVE)
    assert "requires the 'Refunds.Approve' role" in response["value"]
    assert REFUNDS == []


async def test_second_click_on_a_decided_card_does_nothing():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A2", amount=129), reply("done")])
    app, _ = separated(client)
    async with TeamsTestClient(app) as teams:
        await teams.send("refund $129 on A2", user=EVE)
        card = teams.last_card()
        await teams.click(card, "approve", user=BOB)
        again = await teams.click(card, "approve", user=BOB)  # e.g. a stale copy of the card
    assert again["value"]["body"][0]["text"] == "This request was already decided or has expired."
    assert REFUNDS == [("A2", 129.0)]


async def test_message_while_approval_pending_is_answered_not_run():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A2", amount=129)])
    app, _ = separated(client)
    async with TeamsTestClient(app) as teams:
        await teams.send("refund $129 on A2", user=EVE)
        await teams.send("hurry up", user=EVE)
    assert "waiting for a decision on issue_refund" in teams.texts("a:eve")[-1]
    assert len(client.calls) == 1


async def test_reset_starts_a_new_conversation():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="A2", amount=129), reply("fresh start")])
    app, _ = separated(client)
    async with TeamsTestClient(app) as teams:
        await teams.send("refund $129 on A2", user=EVE)
        await teams.send("reset", user=EVE)
        await teams.send("hi", user=EVE)
    assert teams.texts("a:eve")[-2:] == ["Started a new conversation.", "fresh start"]


# --- hardening ---------------------------------------------------------------------------------------


async def test_card_values_are_escaped():
    client = ScriptedChatClient(script=[tool_call("issue_refund", order_id="[click](https://evil.example)",
                                                  amount=80)])
    app, _ = make_app(client)
    async with TeamsTestClient(app) as teams:
        await teams.send("refund", user=ALICE)
        facts = teams.last_card()["body"][2]["items"][1]["facts"]
    assert facts[0]["value"] == r"\[click\]\(https://evil\.example\)"


async def test_malformed_action_is_rejected():
    app, _ = make_app(ScriptedChatClient())
    async with TeamsTestClient(app) as teams:
        fake = {"actions": [{"type": "Action.Execute", "title": "Approve", "verb": VERB_APPROVE,
                             "data": {"session_id": 7, "approval_ids": "x"}}]}
        response = await teams.click(fake, "approve", user=ALICE)
    assert response["statusCode"] == 400


async def test_jwt_is_required_outside_anonymous_mode():
    settings = TeamsSettings(app_id="00000000-0000-0000-0000-000000000001", _env_file=None)
    app, _ = make_app(ScriptedChatClient(), teams=settings)
    async with TeamsTestClient(app) as teams:
        response = await teams.send("hi", user=ALICE)
    assert response.status_code == 401
    assert teams.connector.activities == []


async def test_foreign_service_url_is_refused():
    app, _ = make_app(ScriptedChatClient(script=[reply("hi")]), teams=teams_test_settings(allowed_service_hosts=""))
    async with TeamsTestClient(app) as teams:
        response = await teams.send("hi", user=ALICE)
    assert response.status_code == 401  # 127.0.0.1 isn't a Microsoft host: no token-bearing call to it
    assert teams.connector.activities == []


def test_policy_anonymous_only_local_and_test():
    settings = AgentKitSettings(environment="dev", guardrail_mode="heuristic", _env_file=None)
    with pytest.raises(ValueError, match="only allowed in local and test"):
        create_app(lambda s: None, settings=settings, configure_telemetry=False,
                   channels=[TeamsChannel(teams_test_settings())])


def test_policy_approver_role_needs_a_directory():
    settings = AgentKitSettings(environment="test", guardrail_mode="heuristic", approver_role="X", _env_file=None)
    with pytest.raises(ValueError, match="approver directory"):
        create_app(lambda s: None, settings=settings, configure_telemetry=False,
                   channels=[TeamsChannel(teams_test_settings())])


def test_settings_need_app_id_unless_anonymous():
    with pytest.raises(ValueError, match="APP_ID"):
        TeamsSettings(_env_file=None)


def test_teams_from_env(monkeypatch, tmp_path):
    from agentkit.channels import teams_from_env

    monkeypatch.chdir(tmp_path)  # no .env
    for name in ("AGENTKIT_TEAMS_APP_ID", "AGENTKIT_TEAMS_AUTH_TYPE"):
        monkeypatch.delenv(name, raising=False)
    assert teams_from_env() == []
    monkeypatch.setenv("AGENTKIT_TEAMS_APP_ID", "00000000-0000-0000-0000-000000000001")
    (channel,) = teams_from_env()
    assert channel.teams.app_id == "00000000-0000-0000-0000-000000000001"
