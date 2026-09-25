"""pytest plugin (auto-registered via the ``pytest11`` entry point)."""

from __future__ import annotations

import pytest

from .scripted_client import ScriptedChatClient
from .spans import SpanRecorder, install_span_recorder


@pytest.fixture
def scripted_client() -> ScriptedChatClient:
    """An empty scripted client; call ``.enqueue(...)`` to add model turns."""
    return ScriptedChatClient()


@pytest.fixture
def span_recorder() -> SpanRecorder:
    """Spans emitted during the test (cleared before each test)."""
    recorder = install_span_recorder()
    recorder.clear()
    return recorder
