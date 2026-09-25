"""Build a refusal result that works for both streaming and non-streaming runs."""

from __future__ import annotations

from collections.abc import Sequence

from agent_framework import AgentResponse, AgentResponseUpdate, Content, Message, ResponseStream

__all__ = ["BLOCKED_KEY", "refuse"]

#: ``AgentResponse.additional_properties`` key marking a guardrail refusal (value = reason).
BLOCKED_KEY = "agentkit.blocked"


def refuse(context, message: str, reason: str) -> None:
    """Short-circuit an agent run with ``message``; downstream middleware and the model are skipped."""
    props = {BLOCKED_KEY: reason}
    if context.stream:

        async def _updates():
            yield AgentResponseUpdate(
                role="assistant", contents=[Content.from_text(message)], additional_properties=dict(props)
            )

        def _finalize(updates: Sequence[AgentResponseUpdate]) -> AgentResponse:
            final = AgentResponse.from_updates(updates)
            final.additional_properties = {**(final.additional_properties or {}), **props}
            return final

        context.result = ResponseStream(_updates(), finalizer=_finalize)
    else:
        context.result = AgentResponse(
            messages=[Message(role="assistant", contents=[message])], additional_properties=dict(props)
        )
