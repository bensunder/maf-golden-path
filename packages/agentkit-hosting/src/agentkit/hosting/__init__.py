"""agentkit.hosting — build and host MAF agents with company defaults."""

from .agent import build_agent, default_detector, default_middleware, load_instructions
from .app import ChatRequest, ChatResponseBody, create_app
from .clients import create_chat_client, gateway_headers, get_credential, token_provider
from .sessions import InMemorySessionStore, SessionRecord, SessionStore
from .settings import AgentKitSettings

__all__ = [
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
