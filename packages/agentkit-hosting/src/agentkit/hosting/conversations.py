"""Transport-neutral conversation service: every channel (HTTP API, Teams, AG-UI web chat) runs turns
and approval decisions through here, so session ownership, cross-replica locking, approval rules,
separation of duties and the audit log are enforced in exactly one place.

A channel's only job is to work out *who* is calling (a :class:`Caller`) and to render the result.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from agent_framework import Agent, AgentResponse, AgentResponseUpdate, AgentSession, Message

from agentkit.guardrails import BLOCKED_KEY
from agentkit.telemetry import run_context

from .citations import Citation, citations_from_response
from .approvals import APPROVALS_KEY, AUDIT_KEY, approval_message, approvals_decided, pending_from_response
from .sessions import SessionRecord, SessionStore
from .settings import AgentKitSettings

__all__ = [
    "ApprovalPending",
    "Caller",
    "ConversationError",
    "ConversationService",
    "Decision",
    "InvalidDecisions",
    "NotAllowed",
    "NothingPending",
    "SessionNotFound",
    "TurnResult",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------- types
@dataclass(frozen=True)
class Caller:
    """Who is acting, as established by the channel (Easy Auth headers, Teams activity, ...)."""

    user_id: str | None
    tenant_id: str | None = None
    #: The channel's verdict on whether this caller may approve for others (Entra app role,
    #: Teams approver directory, ...). Only consulted when ``settings.approver_role`` is set.
    is_approver: bool = False
    #: The caller's own access token, for on-behalf-of tools.
    user_assertion: str | None = None
    display_name: str | None = None
    channel: str = "http"


@dataclass(frozen=True)
class Decision:
    approved: bool
    comment: str | None = None


@dataclass
class TurnResult:
    session_id: str
    response: AgentResponse
    pending: list[dict[str, Any]]
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> Literal["completed", "approval_required"]:
        return "approval_required" if self.pending else "completed"

    @property
    def reply(self) -> str:
        return self.response.text or ""

    @property
    def blocked(self) -> str | None:
        return (self.response.additional_properties or {}).get(BLOCKED_KEY)

    @property
    def citations(self) -> list[Citation]:
        """Sources the answer cites with ``[n]`` (from knowledge tools), in citation order."""
        return citations_from_response(self.response)[0]

    @property
    def sources(self) -> list[Citation]:
        """Every source knowledge tools showed the model in this run, cited or not."""
        return citations_from_response(self.response)[1]

    @property
    def usage(self) -> dict[str, Any] | None:
        return dict(self.response.usage_details) if self.response.usage_details else None


class ConversationError(Exception):
    """Base class; ``status`` is the HTTP status the API maps it to."""

    status = 400

    def __init__(self, detail: Any):
        super().__init__(detail if isinstance(detail, str) else str(detail))
        self.detail = detail


class SessionNotFound(ConversationError):
    status = 404


class NotAllowed(ConversationError):
    status = 403


class ApprovalPending(ConversationError):
    status = 409

    def __init__(self, pending: list[dict[str, Any]]):
        super().__init__("an approval is pending for this session")
        self.pending = pending


class NothingPending(ConversationError):
    status = 409


class InvalidDecisions(ConversationError):
    """Unknown ids (400) or not every pending approval decided (409)."""

    def __init__(self, detail: str, status: int):
        super().__init__(detail)
        self.status = status


# ---------------------------------------------------------------------------- service
class ConversationService:
    def __init__(self, agent: Callable[[], Agent], store: SessionStore, settings: AgentKitSettings):
        self._agent = agent
        self.store = store
        self.settings = settings

    # ------------------------------------------------------------ reads and checks
    async def get(self, session_id: str) -> SessionRecord:
        record = await self.store.get(session_id)
        if record is None:
            raise SessionNotFound("session not found or expired")
        return record

    @staticmethod
    def _check_owner(record: SessionRecord, caller: Caller) -> None:
        if record.owner != caller.user_id:
            raise NotAllowed("session belongs to another user")

    @staticmethod
    def _refuse_if_pending(record: SessionRecord | None) -> None:
        if record and record.meta.get(APPROVALS_KEY):
            raise ApprovalPending(record.meta[APPROVALS_KEY])

    async def precheck_turn(self, caller: Caller, session_id: str | None, *, create: bool = False) -> None:
        """Fail fast (before a stream starts) on a missing, foreign or paused session."""
        if not session_id:
            return
        record = await self.store.get(session_id)
        if record is None:
            if create:
                return
            raise SessionNotFound("session not found or expired")
        self._check_owner(record, caller)
        self._refuse_if_pending(record)

    async def pending(self, caller: Caller, session_id: str) -> list[dict[str, Any]]:
        record = await self.get(session_id)
        if record.owner != caller.user_id and not (self.settings.approver_role and caller.is_approver):
            raise NotAllowed("not allowed to view approvals for this session")
        return list(record.meta.get(APPROVALS_KEY, []))

    def check_decider(self, caller: Caller, record: SessionRecord) -> None:
        """Who may decide: an approver (not the requester, with separation of duties) when an approver
        role is configured, otherwise only the requesting user (confirmation)."""
        if self.settings.approver_role:
            if not caller.is_approver:
                raise NotAllowed(f"approving requires the '{self.settings.approver_role}' role")
            if self.settings.approval_separation and caller.user_id == record.owner:
                raise NotAllowed("separation of duties: you cannot approve your own request")
        elif caller.user_id != record.owner:
            raise NotAllowed("only the requesting user can confirm this action")

    @staticmethod
    def _check_decisions(pending: list[dict[str, Any]], decisions: Mapping[str, Decision]) -> None:
        if not pending:
            raise NothingPending("no approval is pending for this session")
        pending_ids = {p["id"] for p in pending}
        unknown = set(decisions) - pending_ids
        if unknown:
            raise InvalidDecisions(f"unknown approval id(s): {sorted(unknown)}", 400)
        missing = pending_ids - set(decisions)
        if missing:
            raise InvalidDecisions(f"decide every pending approval in one request; missing: {sorted(missing)}", 409)

    async def precheck_decision(self, caller: Caller, session_id: str, decisions: Mapping[str, Decision]) -> SessionRecord:
        """Validate a decision without taking the lock or running anything (for channels that must
        answer quickly, e.g. a Teams card click, and resume in the background)."""
        record = await self.get(session_id)
        self.check_decider(caller, record)
        self._check_decisions(record.meta.get(APPROVALS_KEY, []), decisions)
        return record

    # ------------------------------------------------------------ turns
    async def run_turn(
        self,
        caller: Caller,
        message: str | Message,
        session_id: str | None = None,
        *,
        create: bool = False,
        meta: Mapping[str, Any] | None = None,
    ) -> TurnResult:
        """One user turn. ``create=True`` starts a session under ``session_id`` if none exists
        (channels with their own conversation ids); ``meta`` is merged into the session metadata."""
        result: TurnResult | None = None
        async for item in self._turn(caller, message, session_id, create=create, meta=meta, stream=False):
            if isinstance(item, TurnResult):
                result = item
        assert result is not None
        return result

    def stream_turn(
        self,
        caller: Caller,
        message: str | Message,
        session_id: str | None = None,
        *,
        create: bool = False,
        meta: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[AgentResponseUpdate | TurnResult]:
        """Like :meth:`run_turn` but yields MAF updates as they arrive; the last item is the TurnResult."""
        return self._turn(caller, message, session_id, create=create, meta=meta, stream=True)

    async def _turn(self, caller, message, session_id, *, create, meta, stream):
        session_key = session_id or str(uuid.uuid4())
        async with self.store.lock(session_key):
            record = await self.store.get(session_key) if session_id else None
            if session_id and record is None and not create:
                raise SessionNotFound("session not found or expired")
            if record is not None:
                self._check_owner(record, caller)
                self._refuse_if_pending(record)
                session = AgentSession.from_dict(record.data)
            else:
                session = self._agent().create_session(session_id=session_key)
            new_meta = {**(record.meta if record else {}), **(meta or {})}
            with run_context(user_id=caller.user_id, session_id=session_key, tenant_id=caller.tenant_id,
                             request_id=str(uuid.uuid4()), user_assertion=caller.user_assertion):
                response = None
                if stream:
                    updates = self._agent().run(message, session=session, stream=True)
                    async for update in updates:
                        yield update
                    response = await updates.get_final_response()
                else:
                    response = await self._agent().run(message, session=session)
            pending = pending_from_response(response)
            new_meta[APPROVALS_KEY] = pending
            await self._save(session_key, session, caller.user_id, new_meta)
        yield TurnResult(session_key, response, pending, new_meta)

    # ------------------------------------------------------------ decisions
    async def decide(self, caller: Caller, session_id: str, decisions: Mapping[str, Decision]) -> TurnResult:
        result: TurnResult | None = None
        async for item in self._decide(caller, session_id, decisions, stream=False):
            if isinstance(item, TurnResult):
                result = item
        assert result is not None
        return result

    def stream_decide(
        self, caller: Caller, session_id: str, decisions: Mapping[str, Decision]
    ) -> AsyncIterator[AgentResponseUpdate | TurnResult]:
        return self._decide(caller, session_id, decisions, stream=True)

    async def _decide(self, caller, session_id, decisions, *, stream):
        async with self.store.lock(session_id):
            record = await self.get(session_id)
            self.check_decider(caller, record)
            pending: list[dict[str, Any]] = record.meta.get(APPROVALS_KEY, [])
            self._check_decisions(pending, decisions)

            now = time.time()
            audit = list(record.meta.get(AUDIT_KEY, []))
            for item in pending:
                decision = decisions[item["id"]]
                audit.append({"id": item["id"], "tool": item["tool"], "arguments": item["arguments"],
                              "approved": decision.approved, "decided_by": caller.user_id,
                              "decided_by_name": caller.display_name, "channel": caller.channel,
                              "comment": decision.comment, "requested_by": record.owner, "decided_at": now})
                approvals_decided.add(1, {"tool": item["tool"], "decision": "approved" if decision.approved else "rejected"})
                logger.info("approval %s for tool %s: %s", item["id"], item["tool"],
                            "approved" if decision.approved else "rejected")

            session = AgentSession.from_dict(record.data)
            message = approval_message(pending, {k: v.approved for k, v in decisions.items()})
            # The conversation stays attributed to its owner; tools run with the approver's delegated token.
            with run_context(user_id=record.owner, session_id=session_id, tenant_id=caller.tenant_id,
                             request_id=str(uuid.uuid4()), user_assertion=caller.user_assertion,
                             approved_by=caller.user_id or "unknown"):
                if stream:
                    updates = self._agent().run(message, session=session, stream=True)
                    async for update in updates:
                        yield update
                    response = await updates.get_final_response()
                else:
                    response = await self._agent().run(message, session=session)
            next_pending = pending_from_response(response)
            meta = {**record.meta, APPROVALS_KEY: next_pending, AUDIT_KEY: audit}
            await self._save(session_id, session, record.owner, meta)
        yield TurnResult(session_id, response, next_pending, meta)

    # ------------------------------------------------------------ misc
    async def delete(self, caller: Caller, session_id: str) -> None:
        record = await self.store.get(session_id)
        if record is None:
            return
        self._check_owner(record, caller)
        await self.store.delete(session_id)

    async def _save(self, session_id: str, session: AgentSession, owner: str | None, meta: dict[str, Any]) -> None:
        expires = time.time() + self.settings.session_ttl_seconds
        await self.store.put(session_id, SessionRecord(owner=owner, data=session.to_dict(), expires_at=expires, meta=meta))
