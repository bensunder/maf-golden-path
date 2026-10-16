"""``build_agent``: the one function product teams call."""

from __future__ import annotations

import logging

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from agent_framework import Agent, SupportsChatGetResponse, ToolApprovalMiddleware

from agentkit.guardrails import (
    HeuristicInjectionDetector,
    InjectionDetector,
    InputGuardMiddleware,
    PiiRedactionMiddleware,
    PromptShieldsDetector,
    SessionTokenBudgetMiddleware,
    ToolOutputShieldMiddleware,
    ToolPolicyMiddleware,
)
from agentkit.telemetry import AgentRunMetricsMiddleware

from .approvals import EnsureSessionMiddleware
from .clients import create_chat_client, token_provider
from .settings import AgentKitSettings

__all__ = ["build_agent", "default_detector", "default_middleware", "load_instructions", "memory_level"]

logger = logging.getLogger(__name__)


def load_instructions(path: str | Path, **variables: str) -> str:
    """Read versioned instructions (markdown) and fill ``{placeholders}``."""
    text = Path(path).read_text(encoding="utf-8")
    return text.format(**variables) if variables else text


def default_detector(settings: AgentKitSettings) -> InjectionDetector | None:
    if settings.guardrail_mode == "off":
        return None
    if settings.guardrail_mode == "prompt_shields":
        key = settings.content_safety_key.get_secret_value() if settings.content_safety_key else None
        return PromptShieldsDetector(
            settings.content_safety_endpoint or "",
            api_key=key,
            token_provider=None if key else token_provider(settings),
            fail_closed=settings.shields_fail_closed,
        )
    return HeuristicInjectionDetector()


def default_middleware(
    settings: AgentKitSettings,
    *,
    agent_name: str,
    detector: InjectionDetector | None = None,
    tool_policy: ToolPolicyMiddleware | None = None,
    approval_rules: Sequence[Callable[..., Any]] = (),
) -> list[Any]:
    """The paved-road middleware stack, outermost first."""
    detector = detector if detector is not None else default_detector(settings)
    stack: list[Any] = [AgentRunMetricsMiddleware(agent_name=agent_name)]
    if detector is not None:
        stack.append(InputGuardMiddleware(detector, max_input_chars=settings.max_input_chars))
    stack.append(SessionTokenBudgetMiddleware(settings.session_token_budget))
    if approval_rules:
        # Auto-approves low-risk calls to approval_mode="always_require" tools; the rest wait for a human.
        stack.append(EnsureSessionMiddleware())  # MAF's approval middleware requires a session
        stack.append(ToolApprovalMiddleware(auto_approval_rules=list(approval_rules)))
    if settings.redact_pii:
        stack.append(PiiRedactionMiddleware())
    if tool_policy is not None:
        stack.append(tool_policy)
    if detector is not None and settings.scan_tool_output:
        stack.append(ToolOutputShieldMiddleware(detector))
    return stack


def _platform_connectors(settings: AgentKitSettings) -> list[Any]:
    """Connectors assigned by the VPS platform (``AGENTKIT_CONNECTORS``): every agent gets them, with no code."""
    if not settings.connectors:
        return []
    try:
        from agentkit.tools.mcp import platform_connectors
    except ImportError:  # pragma: no cover - agentkit-tools ships with every generated agent
        logger.warning("AGENTKIT_CONNECTORS is set but agentkit-tools isn't installed; no connectors loaded")
        return []
    token = settings.connector_token.get_secret_value() if settings.connector_token else None
    return platform_connectors(settings.connectors, token)


_MEMORY_LEVELS = ("session", "user", "full")


def memory_level(settings: AgentKitSettings, ceiling: str | None = None) -> str:
    """The memory an agent gets: its code's ``memory=`` (if any), lowered by ``AGENTKIT_MEMORY``."""
    env = settings.memory
    return env if ceiling is None else min(env, ceiling, key=_MEMORY_LEVELS.index)


def _platform_memory(settings: AgentKitSettings, providers: Sequence[Any], extra_middleware: Sequence[Any],
                     ceiling: str | None = None) -> tuple[list[Any], Any, str | None]:
    """Long-term memory for every agent (``AGENTKIT_MEMORY``, default ``full``), with no code in the agent.

    Returns ``(providers to add, journal middleware or None, why memory is off or None)``. Providers the agent
    already passes (a project generated with ``memory=user``) are kept and not duplicated. When memory can't
    start (for example prod without ``AGENTKIT_MEMORY_SALT``) the agent still starts, without the missing parts,
    and the reason is logged and shown on the console's Security page."""
    level = memory_level(settings, ceiling)
    if level == "session":
        return [], None, None
    try:
        from agentkit.memory import AgentMemoryProvider, JournalMiddleware, UserMemoryProvider, agent_memory
    except ImportError:
        return [], None, None  # agentkit-memory isn't installed: sessions only
    profiles = {getattr(p, "agentkit_memory_profile", None) for p in providers}
    has_journal = any(isinstance(m, JournalMiddleware) for m in extra_middleware)
    try:
        memory = agent_memory(settings)
    except ValueError as exc:  # configuration refused (salt, store); explicit providers already raised
        logger.error("long-term memory is off: %s", exc)
        return [], None, str(exc)
    added: list[Any] = []
    if "user" not in profiles:
        added.append(UserMemoryProvider(memory.memory))
    journal = None
    if level == "full":
        if "agent" not in profiles:
            added.append(AgentMemoryProvider(memory))
        if not has_journal:
            journal = JournalMiddleware(memory.journal)
    return added, journal, None


def _platform_peers(settings: AgentKitSettings) -> list[Any]:
    """Agents on the VPS platform this agent may call (``AGENTKIT_PEERS``), as tools."""
    if not settings.peers:
        return []
    try:
        from agentkit.tools.peers import platform_peers
    except ImportError:  # pragma: no cover - agentkit-tools ships with every generated agent
        logger.warning("AGENTKIT_PEERS is set but agentkit-tools isn't installed; no peer agents loaded")
        return []
    token = settings.connector_token.get_secret_value() if settings.connector_token else None
    return platform_peers(settings.peers, token)


def build_agent(
    *,
    name: str,
    instructions: str,
    tools: Sequence[Callable[..., Any] | Any] = (),
    settings: AgentKitSettings | None = None,
    client: SupportsChatGetResponse | None = None,
    detector: InjectionDetector | None = None,
    tool_policy: ToolPolicyMiddleware | Mapping[str, Any] | None = None,
    extra_middleware: Sequence[Any] = (),
    approval_rules: Sequence[Callable[..., Any]] = (),
    description: str | None = None,
    memory: Literal["full", "user", "session"] | None = None,
    **agent_kwargs: Any,
) -> Agent:
    """Create a MAF ``Agent`` with company defaults.

    ``client`` is injected in tests (``ScriptedChatClient``); in services it is created from
    settings and bound to the AI gateway. ``tool_policy`` may be a ``ToolPolicyMiddleware`` or
    its keyword arguments (``{"denied": [...], "validators": {...}}``). ``approval_rules`` auto-approve
    matching calls to ``approval_mode="always_require"`` tools (see ``agentkit.hosting.approve_if``).
    ``memory`` is the most long-term memory this agent should have (``full``, ``user`` or ``session``);
    ``AGENTKIT_MEMORY`` can only lower it, so operators keep an off switch (docs/memory.md).
    """
    settings = settings or AgentKitSettings()
    tools = [*tools, *_platform_connectors(settings), *_platform_peers(settings)]
    if isinstance(tool_policy, Mapping):
        tool_policy = ToolPolicyMiddleware(**tool_policy)
    client = client or create_chat_client(settings, agent_name=name)
    middleware = default_middleware(settings, agent_name=name, detector=detector, tool_policy=tool_policy,
                                    approval_rules=approval_rules)
    properties: dict[str, Any] = {"agentkit.service_version": settings.service_version}
    providers = list(agent_kwargs.pop("context_providers", None) or [])
    added, journal, memory_note = _platform_memory(settings, providers, extra_middleware, memory)
    providers.extend(added)
    if journal is not None:
        middleware.insert(1, journal)  # right after run metrics: it also records runs a guardrail refuses
    if memory_note:
        properties["agentkit.memory_off"] = memory_note
    if providers:
        agent_kwargs["context_providers"] = providers
    return Agent(
        client,
        instructions=instructions,
        name=name,
        id=name,
        description=description,
        tools=list(tools),
        middleware=[*middleware, *extra_middleware],
        additional_properties=properties,
        **agent_kwargs,
    )
