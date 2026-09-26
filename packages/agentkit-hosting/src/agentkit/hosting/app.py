"""FastAPI host: probes, chat (JSON + SSE), sessions with ownership and cross-replica locking,
human approvals, request context, and pluggable channels (Teams, AG-UI web chat).

All rules live in :class:`~agentkit.hosting.conversations.ConversationService`; the routes here only
translate HTTP to a :class:`Caller` and back.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal, Protocol, runtime_checkable

from agent_framework import Agent
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agentkit.telemetry import setup_telemetry

from .approvals import install_maf_noise_filter, roles_from_principal
from .conversations import ApprovalPending, Caller, ConversationError, ConversationService, Decision, TurnResult
from .sessions import SessionLockTimeout, SessionStore, session_store_from_settings
from .settings import AgentKitSettings

__all__ = [
    "ApprovalDecision",
    "ApprovalView",
    "Channel",
    "ChatRequest",
    "ChatResponseBody",
    "CitationView",
    "DecisionsRequest",
    "approval_views",
    "create_app",
    "http_caller",
]

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None


class ApprovalView(BaseModel):
    id: str
    tool: str
    arguments: dict[str, Any]
    requested_at: float


class CitationView(BaseModel):
    n: int
    id: str
    title: str
    url: str | None = None


class ChatResponseBody(BaseModel):
    session_id: str
    status: Literal["completed", "approval_required"] = "completed"
    reply: str
    blocked: str | None = None
    approvals: list[ApprovalView] = []
    citations: list[CitationView] = []
    usage: dict[str, Any] | None = None


class ApprovalDecision(BaseModel):
    id: str
    approved: bool
    comment: str | None = Field(default=None, max_length=1000)


class DecisionsRequest(BaseModel):
    decisions: list[ApprovalDecision] = Field(min_length=1)


@runtime_checkable
class Channel(Protocol):
    """A way for users to reach the agent besides the JSON API (Teams, AG-UI web chat, ...).

    ``install`` adds routes; ``startup``/``shutdown`` (optional) run in the app lifespan."""

    def install(self, app: FastAPI, service: ConversationService, settings: AgentKitSettings) -> None: ...


def approval_views(pending: list[dict[str, Any]]) -> list[ApprovalView]:
    return [ApprovalView(id=p["id"], tool=p["tool"], arguments=p["arguments"], requested_at=p["requested_at"]) for p in pending]


def http_caller(request: Request, settings: AgentKitSettings, *, channel: str = "http") -> Caller:
    """The caller behind an HTTP request, from platform-auth headers (Easy Auth / APIM)."""
    user = request.headers.get(settings.user_header)
    if not user and settings.user_fallback_header:
        user = request.headers.get(settings.user_fallback_header)
    if settings.require_user and not user:
        raise HTTPException(401, f"missing {settings.user_header}")
    raw = request.headers.get(settings.user_token_header) if settings.user_token_header else None
    assertion = None
    if raw:
        assertion = raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()
    roles = roles_from_principal(request.headers.get(settings.principal_claims_header))
    return Caller(
        user_id=user,
        tenant_id=request.headers.get(settings.tenant_header),
        is_approver=bool(settings.approver_role) and settings.approver_role in roles,
        user_assertion=assertion,
        channel=channel,
    )


class _JsonOnly:
    """POSTs must be JSON. A browser can't send JSON cross-site without a CORS preflight, so this closes
    the cookie-CSRF door for Easy Auth sessions (web chat) on every FastAPI version."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            content_type = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"content-type"), "")
            media = content_type.split(";")[0].strip().lower()
            if media != "application/json" and not media.endswith("+json"):
                response = JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _body(result: TurnResult) -> ChatResponseBody:
    return ChatResponseBody(
        session_id=result.session_id,
        status=result.status,
        reply=result.reply,
        blocked=result.blocked,
        approvals=approval_views(result.pending),
        citations=[CitationView(**c.to_dict()) for c in result.citations],
        usage=result.usage,
    )


def create_app(
    agent_factory: Callable[[AgentKitSettings], Agent],
    *,
    settings: AgentKitSettings | None = None,
    session_store: SessionStore | None = None,
    configure_telemetry: bool = True,
    channels: Sequence[Channel] = (),
) -> FastAPI:
    """Build the HTTP app. ``agent_factory`` is called once at startup."""
    settings = settings or AgentKitSettings()
    store = session_store or session_store_from_settings(settings)
    state: dict[str, Agent] = {}
    install_maf_noise_filter()

    def _agent() -> Agent:
        agent = state.get("agent")
        if agent is None:
            raise HTTPException(503, "agent not initialised")
        return agent

    service = ConversationService(_agent, store, settings)

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
        for channel in channels:
            if hasattr(channel, "startup"):
                await channel.startup()
        try:
            yield
        finally:
            for channel in reversed(channels):
                if hasattr(channel, "shutdown"):
                    await channel.shutdown()

    app = FastAPI(title=settings.service_name, version=settings.service_version, lifespan=lifespan)
    app.add_middleware(_JsonOnly)
    app.state.session_store = store
    app.state.conversations = service
    app.state.channels = list(channels)

    @app.exception_handler(SessionLockTimeout)
    async def _busy(request: Request, exc: SessionLockTimeout):
        return JSONResponse({"detail": "session is busy with another request; retry shortly"}, status_code=409)

    @app.exception_handler(ConversationError)
    async def _conversation_error(request: Request, exc: ConversationError):
        if isinstance(exc, ApprovalPending):
            views = [v.model_dump() for v in approval_views(exc.pending)]
            return JSONResponse({"detail": {"detail": exc.detail, "approvals": views}}, status_code=exc.status)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    def _caller(request: Request) -> Caller:
        return http_caller(request, settings)

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
        return _body(await service.run_turn(_caller(request), body.message, body.session_id))

    @app.post("/v1/chat/stream")
    async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
        caller = _caller(request)
        await service.precheck_turn(caller, body.session_id)  # 404/403/409 before the stream starts

        async def events():
            result: TurnResult | None = None
            try:
                async for item in service.stream_turn(caller, body.message, body.session_id):
                    if isinstance(item, TurnResult):
                        result = item
                    elif item.text:
                        yield f"data: {json.dumps({'delta': item.text})}\n\n"
            except (ConversationError, SessionLockTimeout) as exc:  # lost a race after the precheck
                yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
                return
            assert result is not None
            if result.pending:
                views = [v.model_dump() for v in approval_views(result.pending)]
                yield f"event: approval_required\ndata: {json.dumps({'approvals': views})}\n\n"
            done = {"session_id": result.session_id, "status": result.status, "blocked": result.blocked,
                    "citations": [c.to_dict() for c in result.citations]}
            yield f"event: done\ndata: {json.dumps(done)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    # ------------------------------------------------------------------ approvals
    @app.get("/v1/sessions/{session_id}/approvals", response_model=list[ApprovalView])
    async def list_approvals(session_id: str, request: Request) -> list[ApprovalView]:
        return approval_views(await service.pending(_caller(request), session_id))

    @app.post("/v1/sessions/{session_id}/approvals", response_model=ChatResponseBody)
    async def decide(session_id: str, body: DecisionsRequest, request: Request) -> ChatResponseBody:
        decisions = {d.id: Decision(approved=d.approved, comment=d.comment) for d in body.decisions}
        return _body(await service.decide(_caller(request), session_id, decisions))

    @app.delete("/v1/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str, request: Request) -> None:
        await service.delete(_caller(request), session_id)
        return None

    for channel in channels:
        channel.install(app, service, settings)

    return app
