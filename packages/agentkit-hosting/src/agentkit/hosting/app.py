"""FastAPI host: health probes, chat (JSON + SSE), session ownership, request context."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from agent_framework import Agent, AgentSession
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agentkit.guardrails import BLOCKED_KEY
from agentkit.telemetry import run_context, setup_telemetry

from .sessions import InMemorySessionStore, SessionRecord, SessionStore
from .settings import AgentKitSettings

__all__ = ["ChatRequest", "ChatResponseBody", "create_app"]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None


class ChatResponseBody(BaseModel):
    session_id: str
    reply: str
    blocked: str | None = None
    usage: dict[str, Any] | None = None


def create_app(
    agent_factory: Callable[[AgentKitSettings], Agent],
    *,
    settings: AgentKitSettings | None = None,
    session_store: SessionStore | None = None,
    configure_telemetry: bool = True,
) -> FastAPI:
    """Build the HTTP app. ``agent_factory`` is called once at startup."""
    settings = settings or AgentKitSettings()
    store = session_store or InMemorySessionStore()
    locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
    state: dict[str, Agent] = {}

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

    async def _load(session_id: str | None, user: str | None) -> tuple[str, AgentSession]:
        agent = _agent()
        if session_id:
            record = await store.get(session_id)
            if record is None:
                raise HTTPException(404, "session not found or expired")
            if record.owner != user:
                raise HTTPException(403, "session belongs to another user")
            return session_id, AgentSession.from_dict(record.data)
        new_id = str(uuid.uuid4())
        return new_id, agent.create_session(session_id=new_id)

    async def _save(session_id: str, session: AgentSession, user: str | None) -> None:
        expires = time.monotonic() + settings.session_ttl_seconds
        await store.put(session_id, SessionRecord(owner=user, data=session.to_dict(), expires_at=expires))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        agent = _agent()
        return {"status": "ready", "agent": agent.name or "", "version": settings.service_version}

    @app.post("/v1/chat", response_model=ChatResponseBody)
    async def chat(body: ChatRequest, request: Request) -> ChatResponseBody:
        user, tenant = _identity(request)
        session_id, session = await _load(body.session_id, user)
        async with locks[session_id]:
            with run_context(user_id=user, session_id=session_id, tenant_id=tenant, request_id=str(uuid.uuid4())):
                result = await _agent().run(body.message, session=session)
            await _save(session_id, session, user)
        return ChatResponseBody(
            session_id=session_id,
            reply=result.text or "",
            blocked=(result.additional_properties or {}).get(BLOCKED_KEY),
            usage=dict(result.usage_details) if result.usage_details else None,
        )

    @app.post("/v1/chat/stream")
    async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
        user, tenant = _identity(request)
        session_id, session = await _load(body.session_id, user)

        async def events():
            async with locks[session_id]:
                with run_context(user_id=user, session_id=session_id, tenant_id=tenant, request_id=str(uuid.uuid4())):
                    stream = _agent().run(body.message, session=session, stream=True)
                    async for update in stream:
                        if update.text:
                            yield f"data: {json.dumps({'delta': update.text})}\n\n"
                    final = await stream.get_final_response()
                await _save(session_id, session, user)
            done = {"session_id": session_id, "blocked": (final.additional_properties or {}).get(BLOCKED_KEY)}
            yield f"event: done\ndata: {json.dumps(done)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

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
