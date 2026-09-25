"""agentkit.hosting — build and host MAF agents with company defaults."""

from .agent import build_agent, default_detector, default_middleware, load_instructions
from .app import ApprovalDecision, ApprovalView, ChatRequest, ChatResponseBody, DecisionsRequest, create_app
from .approvals import approve_if, roles_from_principal
from .clients import create_chat_client, gateway_headers, get_credential, token_provider
from .sessions import (
    CosmosSessionStore,
    InMemorySessionStore,
    RedisSessionStore,
    SessionLockTimeout,
    SessionRecord,
    SessionStore,
    session_store_from_settings,
)
from .settings import AgentKitSettings

__all__ = [
    "ApprovalDecision",
    "ApprovalView",
    "CosmosSessionStore",
    "DecisionsRequest",
    "RedisSessionStore",
    "SessionLockTimeout",
    "approve_if",
    "roles_from_principal",
    "session_store_from_settings",
    "AgentKitSettings",
    "ChatRequest",
    "ChatResponseBody",
    "InMemorySessionStore",
    "SessionRecord",
    "SessionStore",
    "build_agent",
    "create_app",
    "create_chat_client",
    "default_detector",
    "default_middleware",
    "gateway_headers",
    "get_credential",
    "load_instructions",
    "token_provider",
]
