"""Human approval for high-impact tools, on top of MAF's native tool-approval mechanics.

Mark a tool ``@tool(approval_mode="always_require")``. Optionally auto-approve low-risk calls:

    build_agent(..., approval_rules=[approve_if("issue_refund", lambda a: a["amount"] <= 50)])

The host then pauses the run, returns ``status: "approval_required"``, persists the pending
request with the session, and resumes when someone authorised decides via
``POST /v1/sessions/{id}/approvals``.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from agent_framework import AgentMiddleware, AgentResponse, Content, Message
from opentelemetry import metrics

__all__ = [
    "EnsureSessionMiddleware",
    "APPROVALS_KEY",
    "AUDIT_KEY",
    "approval_message",
    "approve_if",
    "pending_from_response",
    "roles_from_principal",
]

APPROVALS_KEY = "approvals"  # SessionRecord.meta key: pending approvals
AUDIT_KEY = "approval_log"  # SessionRecord.meta key: decisions, append-only

_meter = metrics.get_meter("agentkit.hosting")
approvals_requested = _meter.create_counter("agentkit.approvals.requested", unit="{approval}")
approvals_decided = _meter.create_counter("agentkit.approvals.decided", unit="{approval}")


def _arguments(function_call: Content) -> dict[str, Any]:
    args = function_call.arguments
    if isinstance(args, str):
        try:
            return json.loads(args or "{}")
        except ValueError:
            return {"_raw": args}
    return dict(args or {})


def approve_if(tool_name: str, predicate: Callable[[dict[str, Any]], bool]) -> Callable[[Content], bool]:
    """Auto-approval rule: calls to ``tool_name`` whose arguments satisfy ``predicate`` skip the human."""

    def rule(function_call: Content) -> bool:
        if function_call.name != tool_name:
            return False
        try:
            return bool(predicate(_arguments(function_call)))
        except Exception:  # a broken rule must never auto-approve
            return False

    rule.__name__ = f"approve_if_{tool_name}"
    return rule


class EnsureSessionMiddleware(AgentMiddleware):
    """MAF's ToolApprovalMiddleware raises without an AgentSession. Supply a throwaway one so
    ``agent.run("...")`` works in workers and tests. Pass your own session to resume a paused run."""

    async def process(self, context, call_next) -> None:  # type: ignore[override]
        if context.session is None:
            context.session = context.agent.create_session()
        await call_next()


def pending_from_response(response: AgentResponse) -> list[dict[str, Any]]:
    """Approval requests in a run result, in a JSON-safe shape to persist with the session."""
    pending = []
    for request in response.user_input_requests or []:
        if request.type != "function_approval_request" or request.function_call is None:
            continue
        pending.append(
            {
                "id": request.id,
                "tool": request.function_call.name,
                "arguments": _arguments(request.function_call),
                "requested_at": time.time(),
                "request": request.to_dict(),
            }
        )
        approvals_requested.add(1, {"tool": request.function_call.name or ""})
    return pending


def approval_message(pending: Iterable[Mapping[str, Any]], decisions: Mapping[str, bool]) -> Message:
    """The user message that resumes the run with a decision for every pending request."""
    contents = []
    for item in pending:
        request = Content.from_dict(item["request"])
        contents.append(request.to_function_approval_response(approved=bool(decisions[item["id"]])))
    return Message(role="user", contents=contents)


_ROLE_CLAIMS = {"roles", "role", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"}


def roles_from_principal(header_value: str | None) -> set[str]:
    """App roles from Easy Auth's ``X-MS-CLIENT-PRINCIPAL`` (base64 JSON with a ``claims`` list)."""
    if not header_value:
        return set()
    try:
        padded = header_value + "=" * (-len(header_value) % 4)
        principal = json.loads(base64.b64decode(padded))
    except (ValueError, TypeError):
        return set()
    roles: set[str] = set()
    role_type = principal.get("role_typ")
    for claim in principal.get("claims", []):
        if claim.get("typ") in _ROLE_CLAIMS or (role_type and claim.get("typ") == role_type):
            roles.add(str(claim.get("val")))
    return roles


class _MafApprovalNoiseFilter(logging.Filter):
    """MAF 1.19 logs a spurious warning when resuming an approved call.

    The response is bound twice internally: once at the agent layer (which consumes it) and again in
    the function layer (which then can't find it). The call still executes correctly. agentkit
    validates approval ids against the session's pending list *before* resuming, so a genuinely
    mismatched id never reaches MAF. The message is demoted to DEBUG rather than hidden.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.WARNING and "did not match the active approval occurrence" in record.getMessage():
            record.levelno, record.levelname = logging.DEBUG, "DEBUG"
            return logging.getLogger(record.name).isEnabledFor(logging.DEBUG)
        return True


def install_maf_noise_filter() -> None:
    for name in ("agent_framework", "agent_framework._tools"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _MafApprovalNoiseFilter) for f in logger.filters):
            logger.addFilter(_MafApprovalNoiseFilter())
