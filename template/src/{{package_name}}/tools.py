"""Tools the agent can call. Replace the example with your real integrations.

Rules of the road:
* one function per capability, typed parameters with ``Field(description=...)`` so the model
  knows how to call it;
* return short, factual strings or small dicts: the model reads every byte;
* anything that changes state (refunds, emails, tickets) gets ``@tool(approval_mode="always_require")``
  or a validator in ``TOOL_POLICY``;
* call downstream APIs through the gateway or with the service's managed identity. Never embed secrets.

Calling an existing API that has an OpenAPI spec? Don't hand-write the tool. Generate it:

    from agentkit.tools import ApiClient, ManagedIdentityAuth, OnBehalfOfAuth, Shaper, openapi_tools
    ORDERS = ApiClient("https://api.contoso.com/orders", auth=ManagedIdentityAuth("api://orders/.default"))
    TOOLS += openapi_tools("specs/orders.yaml", client=ORDERS, operations=["getOrder"],
                           shapers={"getOrder": Shaper(fields=["id", "status"])})

See docs/connectors.md in the agentkit repo (auth, retries, write opt-in, response shaping, MCP).
"""

from __future__ import annotations

from typing import Annotated

from agent_framework import tool
from pydantic import Field

_FAQ = {
    "hours": "Support is staffed 7am-7pm Mountain Time, Monday to Friday.",
    "escalation": "Urgent issues go to the on-call rotation via the #support-urgent channel.",
}


@tool
def search_faq(topic: Annotated[str, Field(description="Short topic keyword, e.g. 'hours' or 'escalation'")]) -> str:
    """Look up a short answer in the team FAQ."""
    answer = _FAQ.get(topic.strip().lower())
    return answer or f"No FAQ entry for '{topic}'. Known topics: {', '.join(sorted(_FAQ))}."


TOOLS = [search_faq]

# Deny or validate tool calls centrally (see agentkit.guardrails.ToolPolicyMiddleware).
TOOL_POLICY: dict = {"denied": [], "validators": {}}
