import os
from pathlib import Path

import pytest
from agentkit.hosting import AgentKitSettings
from agentkit.knowledge import StaticGroups
from agentkit.knowledge.testing import fake_knowledge, index_folder
from agentkit.tools.testing import mock_api

from order_status_agent.connectors import CARRIER
from order_status_agent.tools import KNOWLEDGE

LIVE = os.getenv("AGENTKIT_LIVE_EVALS") == "1"

# What the fake carrier API returns in offline tests and evals.
CARRIER_ROUTES = {
    "GET /shipments/1Z999AA10123456784": {
        "trackingNumber": "1Z999AA10123456784",
        "status": "out_for_delivery",
        "destination": {"city": "Lehi", "line1": "1 Main St", "postalCode": "84043"},
        "internalRoutingCode": "HUB-7-SLC",
    },
}

REFUND_LEADS = "6f1c2a4e-0000-4000-8000-00000000a11d"  # the refund-leads group in knowledge/acl.yaml
#: Offline stand-ins for the users in evals/cases.yaml. Live runs look them up in Entra: use real test accounts.
EVAL_USERS = StaticGroups({"sam@contoso.example": [], "riley@contoso.example": [REFUND_LEADS]})


@pytest.fixture
def settings() -> AgentKitSettings:
    """Offline settings: no gateway, heuristic guardrails, nothing read from .env."""
    return AgentKitSettings(environment="test", guardrail_mode="heuristic", _env_file=None)


@pytest.fixture(autouse=True)
def carrier_api():
    """Offline, every test talks to a fake carrier API. Live evals use the real one."""
    if LIVE:
        yield None
        return
    with mock_api(CARRIER, CARRIER_ROUTES) as calls:
        yield calls


@pytest.fixture(scope="session")
def knowledge_index():
    """knowledge/ run through the real ingestion pipeline (acl.yaml, extraction, chunking) into a fake index."""
    return index_folder(Path(__file__).parents[1] / "knowledge")


@pytest.fixture(autouse=True)
def offline_knowledge(knowledge_index):
    """Offline, every test searches the fake index as the EVAL_USERS. Live evals use the real index."""
    if LIVE:
        yield None
        return
    with fake_knowledge(KNOWLEDGE, knowledge_index, groups=EVAL_USERS) as index:
        yield index
