"""FastAPI host: probes, chat (JSON + SSE), sessions with ownership and cross-replica locking,
human approvals, request context."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

from agent_framework import Agent, AgentResponse, AgentSession
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agentkit.guardrails import BLOCKED_KEY
from agentkit.telemetry import run_context, setup_telemetry

from .approvals import (
    APPROVALS_KEY,
    AUDIT_KEY,
    approval_message,
    approvals_decided,
    install_maf_noise_filter,
    pending_from_response,
    roles_from_principal,
)
from .sessions import SessionLockTimeout, SessionRecord, SessionStore, session_store_from_settings
from .settings import AgentKitSettings

__all__ = ["ApprovalDecision", "ApprovalView", "ChatRequest", "ChatResponseBody", "DecisionsRequest", "create_app"]

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None


class ApprovalView(BaseModel):
    id: str
    tool: str
    arguments: dict[str, Any]
    requested_at: float


class ChatResponseBody(BaseModel):
    session_id: str
    status: Literal["completed", "approval_required"] = "completed"
    reply: str
    blocked: str | None = None
    approvals: list[ApprovalView] = []
    usage: dict[str, Any] | None = None


class ApprovalDecision(BaseModel):
    id: str
    approved: bool
    comment: str | None = Field(default=None, max_length=1000)


class DecisionsRequest(BaseModel):
    decisions: list[ApprovalDecision] = Field(min_length=1)


def _views(pending: list[dict[str, Any]]) -> list[ApprovalView]:
    return [ApprovalView(id=p["id"], tool=p["tool"], arguments=p["arguments"], requested_at=p["requested_at"]) for p in pending]


def create_app(
    agent_factory: Callable[[AgentKitSettings], Agent],
    *,
    settings: AgentKitSettings | None = None,
    session_store: SessionStore | None = None,
    configure_telemetry: bool = True,
) -> FastAPI:
    """Build the HTTP app. ``agent_factory`` is called once at startup."""
    settings = settings or AgentKitSettings()
    store = session_store or session_store_from_settings(settings)
    state: dict[str, Agent] = {}
    install_maf_noise_filter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if configure_telemetry:
            setup_telemetry(
                service_name=settings.service_name,
                service_version=settings.service_version,
                environment=settings.environment,
                team=settings.team,
                otlp_endpoint=settings.otlp_endpoint,
                appinsights_connection_string=settings.appinsights_connection_string,
                capture_message_content=settings.capture_message_content,
            )
        state["agent"] = agent_factory(settings)
        yield

    app = FastAPI(title=settings.service_name, version=settings.service_version, lifespan=lifespan)
    app.state.session_store = store

    @app.exception_handler(SessionLockTimeout)
    async def _busy(request: Request, exc: SessionLockTimeout):
        return JSONResponse({"detail": "session is busy with another request; retry shortly"}, status_code=409)

    # ------------------------------------------------------------------ helpers
    def _agent() -> Agent:
        agent = state.get("agent")
        if agent is None:
            raise HTTPException(503, "agent not initialised")
        return agent

    def _identity(request: Request) -> tuple[str | None, str | None]:
        user = request.headers.get(settings.user_header)
        if settings.require_user and not user:
            raise HTTPException(401, f"missing {settings.user_header}")
        return user, request.headers.get(settings.tenant_header)

    def _user_assertion(request: Request) -> str | None:
        raw = request.headers.get(settings.user_token_header) if settings.user_token_header else None
        if not raw:
            return None
        return raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()

    def _is_approver(request: Request) -> bool:
        return bool(settings.approver_role) and settings.approver_role in roles_from_principal(
            request.headers.get(settings.principal_claims_header)
        )

    async def _load(session_id: str | None, user: str | None, new_id: str) -> tuple[str, AgentSession, SessionRecord | None]:
        if session_id:
            record = await store.get(session_id)
            if record is None:
                raise HTTPException(404, "session not found or expired")
            if record.owner != user:
                raise HTTPException(403, "session belongs to another user")
            return session_id, AgentSession.from_dict(record.data), record
        return new_id, _agent().create_session(session_id=new_id), None

    async def _save(session_id: str, session: AgentSession, owner: str | None, meta: dict[str, Any]) -> None:
        expires = time.time() + settings.session_ttl_seconds
        await store.put(session_id, SessionRecord(owner=owner, data=session.to_dict(), expires_at=expires, meta=meta))

    def _respond(session_id: str, result: AgentResponse, pending: list[dict[str, Any]]) -> ChatResponseBody:
        return ChatResponseBody(
            session_id=session_id,
            status="approval_required" if pending else "completed",
            reply=result.text or "",
            blocked=(result.additional_properties or {}).get(BLOCKED_KEY),
            approvals=_views(pending),
            usage=dict(result.usage_details) if result.usage_details else None,
        )

    def _refuse_if_pending(record: SessionRecord | None) -> None:
        if record and record.meta.get(APPROVALS_KEY):
            raise HTTPException(409, {"detail": "an approval is pending for this session",
                                      "approvals": [v.model_dump() for v in _views(record.meta[APPROVALS_KEY])]})

    # ------------------------------------------------------------------ probes
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        agent = _agent()
        return {"status": "ready", "agent": agent.name or "", "version": settings.service_version}

    # ------------------------------------------------------------------ chat
    @app.post("/v1/chat", response_model=ChatResponseBody)
    async def chat(body: ChatRequest, request: Request) -> ChatResponseBody:
        user, tenant = _identity(request)
        session_key = body.session_id or str(uuid.uuid4())
        async with store.lock(session_key):
            session_id, session, record = await _load(body.session_id, user, session_key)
            _refuse_if_pending(record)
            meta = dict(record.meta) if record else {}
            with run_context(user_id=user, session_id=session_id, tenant_id=tenant, request_id=str(uuid.uuid4()),
                             user_assertion=_user_assertion(request)):
                result = await _agent().run(body.message, session=session)
            pending = pending_from_response(result)
            meta[APPROVALS_KEY] = pending
            await _save(session_id, session, user, meta)
        return _respond(session_id, result, pending)

    @app.post("/v1/chat/stream")
    async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
        user, tenant = _identity(request)
        if body.session_id:  # fail fast (404/403/409) before the stream starts
            record = await store.get(body.session_id)
            if record is None:
                raise HTTPException(404, "session not found or expired")
            if record.owner != user:
                raise HTTPException(403, "session belongs to another user")
            _refuse_if_pending(record)
        session_key = body.session_id or str(uuid.uuid4())
        assertion = _user_assertion(request)

        async def events():
            async with store.lock(session_key):
                session_id, session, record = await _load(body.session_id, user, session_key)
                meta = dict(record.meta) if record else {}
                with run_context(user_id=user, session_id=session_id, tenant_id=tenant, request_id=str(uuid.uuid4()),
                                 user_assertion=assertion):
                    stream = _agent().run(body.message, session=session, stream=True)
                    async for update in stream:
                        if update.text:
                            yield f"data: {json.dumps({'delta': update.text})}\n\n"
                    final = await stream.get_final_response()
                pending = pending_from_response(final)
                meta[APPROVALS_KEY] = pending
                await _save(session_id, session, user, meta)
            if pending:
                views = [v.model_dump() for v in _views(pending)]
                yield f"event: approval_required\ndata: {json.dumps({'approvals': views})}\n\n"
            done = {"session_id": session_id, "status": "approval_required" if pending else "completed",
                    "blocked": (final.additional_properties or {}).get(BLOCKED_KEY)}
            yield f"event: done\ndata: {json.dumps(done)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    # ------------------------------------------------------------------ approvals
    @app.get("/v1/sessions/{session_id}/approvals", response_model=list[ApprovalView])
    async def list_approvals(session_id: str, request: Request) -> list[ApprovalView]:
        user, _ = _identity(request)
        record = await store.get(session_id)
        if record is None:
            raise HTTPException(404, "session not found or expired")
        if record.owner != user and not _is_approver(request):
            raise HTTPException(403, "not allowed to view approvals for this session")
        return _views(record.meta.get(APPROVALS_KEY, []))

    @app.post("/v1/sessions/{session_id}/approvals", response_model=ChatResponseBody)
    async def decide(session_id: str, body: DecisionsRequest, request: Request) -> ChatResponseBody:
        approver, tenant = _identity(request)
        async with store.lock(session_id):
            record = await store.get(session_id)
            if record is None:
                raise HTTPException(404, "session not found or expired")
            if settings.approver_role:
                if not _is_approver(request):
                    raise HTTPException(403, f"approving requires the '{settings.approver_role}' role")
                if settings.approval_separation and approver == record.owner:
                    raise HTTPException(403, "separation of duties: you cannot approve your own request")
            elif approver != record.owner:
                raise HTTPException(403, "only the requesting user can confirm this action")

            pending: list[dict[str, Any]] = record.meta.get(APPROVALS_KEY, [])
            if not pending:
                raise HTTPException(409, "no approval is pending for this session")
            pending_ids = {p["id"] for p in pending}
            decided = {d.id: d for d in body.decisions}
            unknown = set(decided) - pending_ids
            if unknown:
                raise HTTPException(400, f"unknown approval id(s): {sorted(unknown)}")
            missing = pending_ids - set(decided)
            if missing:
                raise HTTPException(409, f"decide every pending approval in one request; missing: {sorted(missing)}")

            now = time.time()
            audit = list(record.meta.get(AUDIT_KEY, []))
            for item in pending:
                decision = decided[item["id"]]
                audit.append({"id": item["id"], "tool": item["tool"], "arguments": item["arguments"],
                              "approved": decision.approved, "decided_by": approver, "comment": decision.comment,
                              "requested_by": record.owner, "decided_at": now})
                approvals_decided.add(1, {"tool": item["tool"], "decision": "approved" if decision.approved else "rejected"})
                logger.info("approval %s for tool %s: %s", item["id"], item["tool"],
                            "approved" if decision.approved else "rejected")

            session = AgentSession.from_dict(record.data)
            message = approval_message(pending, {k: v.approved for k, v in decided.items()})
            # The conversation stays attributed to its owner; tools run with the approver's delegated token.
            with run_context(user_id=record.owner, session_id=session_id, tenant_id=tenant,
                             request_id=str(uuid.uuid4()), user_assertion=_user_assertion(request),
                             approved_by=approver or "unknown"):
                result = await _agent().run(message, session=session)
            next_pending = pending_from_response(result)
            meta = {**record.meta, APPROVALS_KEY: next_pending, AUDIT_KEY: audit}
            await _save(session_id, session, record.owner, meta)
        return _respond(session_id, result, next_pending)

    @app.delete("/v1/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str, request: Request) -> None:
        user, _ = _identity(request)
        record = await store.get(session_id)
        if record is None:
            return None
        if record.owner != user:
            raise HTTPException(403, "session belongs to another user")
        await store.delete(session_id)
        return None

    return app
