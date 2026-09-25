import os

import pytest
from agentkit.hosting import AgentKitSettings
from agentkit.tools.testing import mock_api

from order_status_agent.connectors import CARRIER

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
