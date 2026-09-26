"""HTTP entrypoint: ``uvicorn order_status_agent.app:app``.

Web: AG-UI at /v1/agui and a chat page at /chat, and the operations console at /console, next to the
JSON API (/v1/chat, approvals).

Teams: the bot endpoint /api/messages is on once AGENTKIT_TEAMS_APP_ID is set (the Bicep sets it).
"""

from agentkit.channels import AgUiChannel, Console, WebChat, teams_from_env
from agentkit.hosting import create_app

from .agent import create_agent


def channels() -> list:
    """Ways users reach the agent besides the JSON API; all share its sessions, approvals and audit log."""
    result: list = []
    result += [AgUiChannel(), WebChat(title="Order Status Agent")]
    result += [Console(title="Order Status Agent")]
    result += teams_from_env()
    return result


app = create_app(create_agent, channels=channels())
