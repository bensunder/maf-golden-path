"""Team-written: large refunds are approved by a refunds lead in Teams, never by the requester."""

import pytest
from agentkit.channels import StaticApprovers, TeamsChannel
from agentkit.channels.testing import TeamsTestClient, TeamsTestUser, teams_test_settings
from agentkit.hosting import AgentKitSettings, create_app
from agentkit.testing import ScriptedChatClient, reply, tool_call

from order_status_agent import create_agent
from order_status_agent import tools as t

AGENT, LEAD = TeamsTestUser("sam"), TeamsTestUser("riley")  # a support agent and a refunds lead
LEADS_CHANNEL = "19:refund-leads@thread.tacv2"


@pytest.fixture(autouse=True)
def _reset():
    t.REFUNDS.clear()


def app_for(client):
    settings = AgentKitSettings(environment="test", guardrail_mode="heuristic", approver_role="Refunds.Approve",
                                _env_file=None)
    teams = TeamsChannel(teams_test_settings(approvals_channel_id=LEADS_CHANNEL),
                         approvers=StaticApprovers([LEAD.object_id]))
    return create_app(lambda s: create_agent(s, client=client), settings=settings, configure_telemetry=False,
                      channels=[teams])


async def test_large_refund_is_approved_by_a_lead_in_the_leads_channel():
    client = ScriptedChatClient(script=[
        reply("Noted."),  # the lead's message that registers the leads channel
        tool_call("lookup_order", order_id="A1002"),
        tool_call("issue_refund", order_id="A1002", amount=129, reason="changed mind"),
        reply("The $129.00 refund on A1002 is done."),
    ])
    async with TeamsTestClient(app_for(client)) as teams:
        await teams.send("hi, this is the leads channel", user=LEAD, channel_id=LEADS_CHANNEL)
        await teams.send("Refund the full $129 on A1002, they changed their mind", user=AGENT)
        card = teams.last_card(LEADS_CHANNEL)
        facts = {f["title"]: f["value"] for f in card["body"][2]["items"][1]["facts"]}
        assert facts["amount"] == "129" and facts["order\\_id"] == "A1002"
        assert t.REFUNDS == []

        denied = await teams.click(card, "approve", user=AGENT)  # the requester tries to approve
        assert "role" in denied["value"] and t.REFUNDS == []

        await teams.click(card, "approve", user=LEAD)
    assert t.REFUNDS == [{"order_id": "A1002", "amount": 129.0, "reason": "changed mind"}]
    assert teams.texts("a:sam")[-1] == "Approved by Riley. The $129.00 refund on A1002 is done."


async def test_small_refund_needs_no_card():
    client = ScriptedChatClient(script=[
        tool_call("lookup_order", order_id="A1001"),
        tool_call("issue_refund", order_id="A1001", amount=10, reason="dented box"),
        reply("Refunded $10.00 on A1001."),
    ])
    async with TeamsTestClient(app_for(client)) as teams:
        await teams.send("Refund $10 on A1001, dented box", user=AGENT)
    assert teams.cards() == []
    assert t.REFUNDS == [{"order_id": "A1001", "amount": 10.0, "reason": "dented box"}]
