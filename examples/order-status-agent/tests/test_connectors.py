"""The carrier connector: generated from OpenAPI, shaped, read-only."""

from agentkit.testing import ScriptedChatClient, reply, tool_call

from order_status_agent import create_agent
from order_status_agent.tools import TOOLS


def test_only_read_operations_are_exposed():
    names = {t.name for t in TOOLS}
    assert "get_shipment" in names
    assert "redirect_shipment" not in names  # a write in the spec, deliberately not exposed


async def test_live_tracking_is_shaped_for_the_model(settings, carrier_api):
    client = ScriptedChatClient(
        script=[tool_call("get_shipment", trackingNumber="1Z999AA10123456784"), reply("It's out for delivery in Lehi.")]
    )
    result = await create_agent(settings, client=client).run("Where is 1Z999AA10123456784 right now?")
    assert "out for delivery" in result.text
    seen_by_model = str(list(client.tool_results().values())[0])
    assert "out_for_delivery" in seen_by_model and "Lehi" in seen_by_model
    assert "HUB-7-SLC" not in seen_by_model and "1 Main St" not in seen_by_model  # shaper dropped internals/PII
    assert carrier_api[0].url.path == "/v1/shipments/1Z999AA10123456784"


async def test_carrier_outage_is_explained_not_crashed(settings, carrier_api):
    client = ScriptedChatClient(
        script=[tool_call("get_shipment", trackingNumber="UNKNOWN1"), reply("The carrier has no record of that number.")]
    )
    await create_agent(settings, client=client).run("track UNKNOWN1")
    assert "HTTP 404" in str(list(client.tool_results().values())[0])
