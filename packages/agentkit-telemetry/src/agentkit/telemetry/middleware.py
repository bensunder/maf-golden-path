"""Run-level metrics that MAF does not emit on its own."""

from __future__ import annotations

import time

from agent_framework import AgentMiddleware, AgentResponse, ResponseStream
from opentelemetry import metrics

__all__ = ["AgentRunMetricsMiddleware", "BLOCKED_KEY"]

#: Guardrails put this key in ``AgentResponse.additional_properties`` when they refuse.
BLOCKED_KEY = "agentkit.blocked"

_meter = metrics.get_meter("agentkit.telemetry")
_runs = _meter.create_counter("agentkit.agent.runs", unit="{run}", description="Agent runs by outcome")
_duration = _meter.create_histogram("agentkit.agent.run.duration", unit="s", description="End-to-end agent run time")


def _outcome(result: AgentResponse | None) -> tuple[str, str | None]:
    """(outcome, blocked reason). The reason is the guardrail's code without its detail
    (``prompt_injection:prompt_shields`` → ``prompt_injection``) so the dimension stays low-cardinality."""
    if result is None:
        return "error", None
    blocked = (result.additional_properties or {}).get(BLOCKED_KEY)
    if blocked:
        return "blocked", str(blocked).split(":", 1)[0][:40]
    return "ok", None


class AgentRunMetricsMiddleware(AgentMiddleware):
    """Counts runs by outcome (ok/blocked/error) and records duration per agent.

    Place it first in the middleware list so it also sees runs that guardrails refuse.
    """

    def __init__(self, *, agent_name: str | None = None) -> None:
        self._agent_name = agent_name

    async def process(self, context, call_next) -> None:  # type: ignore[override]
        name = self._agent_name or getattr(context.agent, "name", None) or "agent"
        started = time.perf_counter()

        def record(outcome: tuple[str, str | None]) -> None:
            attrs = {"gen_ai.agent.name": name, "outcome": outcome[0], "stream": context.stream}
            if outcome[1]:
                attrs["blocked_reason"] = outcome[1]
            _runs.add(1, attrs)
            _duration.record(time.perf_counter() - started, attrs)

        if context.stream:
            # MAF applies these to the ResponseStream once the pipeline returns.
            def _on_final(final: AgentResponse) -> AgentResponse:
                record(_outcome(final))
                return final

            context.stream_result_hooks.append(_on_final)

        try:
            await call_next()
        except Exception:
            record(("error", None))
            raise

        if not isinstance(context.result, ResponseStream):
            record(_outcome(context.result))
