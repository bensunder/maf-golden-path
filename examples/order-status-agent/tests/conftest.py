import pytest
from agentkit.hosting import AgentKitSettings


@pytest.fixture
def settings() -> AgentKitSettings:
    """Offline settings: no gateway, heuristic guardrails, nothing read from .env."""
    return AgentKitSettings(environment="test", guardrail_mode="heuristic", _env_file=None)
