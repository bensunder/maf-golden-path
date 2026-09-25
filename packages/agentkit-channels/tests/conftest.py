import pytest
from agent_framework import tool

from agentkit.hosting import SOURCES_HEADER, format_source


@tool
def search_docs(query: str) -> str:
    """Search documents (test double emitting agentkit's shared source format)."""
    return SOURCES_HEADER + "\n\n" + "\n\n".join([
        format_source(1, "refund-policy", "Refund policy", "https://intranet.example/refunds", "Refunds over $50..."),
        format_source(2, "evil-doc", "Click me", "javascript:alert(1)", "text"),
    ])


@pytest.fixture
def docs_tool():
    return search_docs
