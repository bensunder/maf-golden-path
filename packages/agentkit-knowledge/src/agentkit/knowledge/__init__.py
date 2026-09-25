"""agentkit.knowledge — answer from company documents, as the signed-in user, with citations.

Retrieval is trimmed by the caller's Entra groups and fails closed. Sources come back in agentkit's
shared format, so every channel shows citations and evals can check them.
"""

from .search import (
    GraphGroups,
    GroupResolver,
    KnowledgeBase,
    KnowledgeSettings,
    KnowledgeUnavailable,
    Passage,
    StaticGroups,
    gateway_embedder,
    security_filter,
)
from .tool import KNOWLEDGE_INSTRUCTIONS, knowledge_tool

__all__ = [
    "KNOWLEDGE_INSTRUCTIONS",
    "GraphGroups",
    "GroupResolver",
    "KnowledgeBase",
    "KnowledgeSettings",
    "KnowledgeUnavailable",
    "Passage",
    "StaticGroups",
    "gateway_embedder",
    "knowledge_tool",
    "security_filter",
]
