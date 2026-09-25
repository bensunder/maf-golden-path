"""agentkit.testing — offline test doubles and eval runner for MAF agents."""

from .evals import BLOCKED_KEY, CaseResult, EvalCase, load_eval_cases, run_case, run_suite
from .scripted_client import (
    ScriptedChatClient,
    ScriptExhaustedError,
    ToolCall,
    Turn,
    reply,
    tool_call,
)
from .spans import SpanRecorder, install_span_recorder

__all__ = [
    "BLOCKED_KEY",
    "CaseResult",
    "EvalCase",
    "ScriptExhaustedError",
    "ScriptedChatClient",
    "SpanRecorder",
    "ToolCall",
    "Turn",
    "install_span_recorder",
    "load_eval_cases",
    "reply",
    "run_case",
    "run_suite",
    "tool_call",
]
