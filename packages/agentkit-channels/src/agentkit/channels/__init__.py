"""agentkit.channels — reach an agentkit service from Microsoft Teams and AG-UI web chat.

Every channel runs through agentkit.hosting's ConversationService, so sessions, ownership, locking,
approvals (with separation of duties) and the audit log behave exactly as in the JSON API.
"""

from .agui import AgUiChannel, agui_thread_session_id
from .cards import approval_card, decided_card, notice_card, plain
from .teams import (
    ApproverDirectory,
    GraphGroupApprovers,
    StaticApprovers,
    TeamsChannel,
    TeamsSettings,
    TeamsUser,
    teams_from_env,
    teams_session_id,
)
from .webchat import WebChat

__all__ = [
    "AgUiChannel",
    "agui_thread_session_id",
    "ApproverDirectory",
    "GraphGroupApprovers",
    "StaticApprovers",
    "TeamsChannel",
    "TeamsSettings",
    "TeamsUser",
    "WebChat",
    "approval_card",
    "decided_card",
    "notice_card",
    "plain",
    "teams_from_env",
    "teams_session_id",
]
