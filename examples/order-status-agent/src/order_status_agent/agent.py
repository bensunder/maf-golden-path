"""Order Status Agent: agent definition.

Most changes to this service happen in ``tools.py`` and ``instructions/system.md``.
Model access, auth, guardrails, telemetry, sessions and run limits come from agentkit.
"""

from __future__ import annotations

from importlib.resources import files

from agent_framework import Agent, SupportsChatGetResponse
from agentkit.hosting import AgentKitSettings, build_agent, load_instructions

from .tools import TOOL_POLICY, TOOLS

AGENT_NAME = "order-status-agent"
DESCRIPTION = "Answers order, shipping and small-refund questions for customer support staff."
INSTRUCTIONS = files(__package__) / "instructions" / "system.md"


def create_agent(settings: AgentKitSettings | None = None, client: SupportsChatGetResponse | None = None) -> Agent:
    """Build the agent. Tests pass a ``ScriptedChatClient``; the service uses the gateway client."""
    settings = settings or AgentKitSettings()
    return build_agent(
        name=AGENT_NAME,
        description=DESCRIPTION,
        instructions=load_instructions(INSTRUCTIONS),
        tools=TOOLS,
        tool_policy=TOOL_POLICY,
        settings=settings,
        client=client,
    )
