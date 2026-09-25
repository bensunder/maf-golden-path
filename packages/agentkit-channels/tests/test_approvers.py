"""Approver directories used by the Teams channel."""

import httpx

from agentkit.channels import GraphGroupApprovers, StaticApprovers, TeamsUser
from agentkit.tools import ApiClient
from agentkit.tools.testing import mock_api

GROUP = "11111111-2222-3333-4444-555555555555"


def user(oid):
    return TeamsUser(id="29:x", aad_object_id=oid, name="X", tenant_id="t")


async def test_static_approvers_match_object_ids_case_insensitively():
    directory = StaticApprovers(["OID-BOB", " oid-alice "])
    assert await directory.is_approver(user("oid-bob"))
    assert await directory.is_approver(user("oid-alice"))
    assert not await directory.is_approver(user("oid-eve"))
    assert not await directory.is_approver(user(None))


async def test_graph_group_membership_is_checked_and_cached():
    client = ApiClient("https://graph.microsoft.com/v1.0")
    directory = GraphGroupApprovers(GROUP, client=client)
    with mock_api(client, {"POST /users/oid-bob/checkMemberGroups": {"value": [GROUP]},
                           "POST /users/oid-eve/checkMemberGroups": {"value": []}}) as calls:
        assert await directory.is_approver(user("oid-bob"))
        assert await directory.is_approver(user("oid-bob"))  # cached
        assert not await directory.is_approver(user("oid-eve"))
    assert len(calls) == 2
    assert calls[0].read() == b'{"groupIds":["' + GROUP.encode() + b'"]}'


async def test_graph_errors_fail_closed_and_are_not_cached():
    client = ApiClient("https://graph.microsoft.com/v1.0")
    directory = GraphGroupApprovers(GROUP, client=client)
    with mock_api(client, lambda request: httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied"}})):
        assert not await directory.is_approver(user("oid-bob"))
    with mock_api(client, {"POST /users/oid-bob/checkMemberGroups": {"value": [GROUP]}}):
        assert await directory.is_approver(user("oid-bob"))
