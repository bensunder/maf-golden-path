import socket
import threading
import time

import pytest
import uvicorn
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


# ------------------------------------------------------------------ browser tests
@pytest.fixture
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browsers not installed
            pytest.skip(f"no Chromium for Playwright: {exc}")
        yield b
        b.close()


class Server:
    def __init__(self, app):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


