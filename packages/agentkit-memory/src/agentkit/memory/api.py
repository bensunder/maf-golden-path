"""``MemoryChannel``: the versioned HTTP API for people to see and control what an agent remembers.

Every route acts on the caller's own memory only, identified the same way as chat (platform auth headers),
so there is no route that can read another person's memory.

    GET    /v1/memory              200 MemoryListOut
    POST   /v1/memory              201 created | 200 already remembered or replayed   (Idempotency-Key required)
    PATCH  /v1/memory/{id}         200 MemoryItemOut
    DELETE /v1/memory/{id}         204 (idempotent)
    DELETE /v1/memory              204 forget everything
    GET    /v1/memory/export       200 text/markdown

The agent's own memory, for memory admins (``AGENTKIT_MEMORY_ADMINS``, or the approver role where roles exist):

    GET    /v1/memory/agent                              200 who may do what, and counts (any signed-in user)
    GET    /v1/memory/agent/lessons[?status=]            200 lessons, newest first
    POST   /v1/memory/agent/lessons                      201 an admin's lesson (active at once)
    PATCH  /v1/memory/agent/lessons/{id}                 200 edited
    POST   /v1/memory/agent/lessons/{id}/approve         200 pending -> active
    POST   /v1/memory/agent/lessons/{id}/reject          200 pending -> archived
    POST   /v1/memory/agent/lessons/{id}/restore         200 archived -> active
    POST   /v1/memory/agent/lessons/{id}/revert          200 back to an earlier revision's text
    DELETE /v1/memory/agent/lessons/{id}                 204 archived (restorable)
    GET    /v1/memory/agent/subjects                     200 subjects with history, newest first
    GET    /v1/memory/agent/subjects/{subject}           200 that subject's events
    DELETE /v1/memory/agent/subjects/{subject}           204 its history erased
    GET    /v1/memory/agent/journal[?hours=&subject=]    200 what recent runs did

With analytics on (``AGENTKIT_ANALYTICS_ENABLED``), ``/v1/analytics`` is mounted too (``analytics/api.py``).

A kind of memory the agent doesn't have answers ``404 memory_off``; a non-admin gets ``403 not_memory_admin``.

Errors: ``{"detail": {"code": ..., "message": ...}}`` with 401, 404, 409, 422, 429 (Retry-After) or
503 (Retry-After). POST bodies must be JSON (the host's CSRF rule); PATCH and DELETE can't be sent
cross-site without a CORS preflight.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from agentkit.hosting.app import http_caller

from .errors import MemoryError_, MemoryRateLimited, MemoryUnavailable
from .models import (
    IDEMPOTENCY_KEY_PATTERN,
    ITEM_ID_PATTERN,
    LESSON_ID_PATTERN,
    SUBJECT_PATTERN,
    JournalEntry,
    Lesson,
    LessonStatus,
    MemoryItemIn,
    MemoryItemOut,
    MemoryItemPatch,
    MemoryListOut,
    SubjectEvent,
)
from .service import MemoryService, MemoryUser

__all__ = ["MemoryChannel"]


class _ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorBody(BaseModel):
    detail: _ErrorDetail


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status, headers=headers)


_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorBody, "description": "No signed-in user"},
    422: {"model": ErrorBody, "description": "Invalid request, or the text breaks a memory rule"},
    429: {"model": ErrorBody, "description": "Too many changes; see Retry-After"},
    503: {"model": ErrorBody, "description": "Memory store unavailable; see Retry-After"},
}


class LessonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)


class RevertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0, le=49)


class LessonListOut(BaseModel):
    items: list[Lesson]
    counts: dict[str, int]


class SubjectRef(BaseModel):
    subject: str
    last_at: float


class SubjectListOut(BaseModel):
    items: list[SubjectRef]


class SubjectHistoryOut(BaseModel):
    subject: str
    events: list[SubjectEvent]


class JournalOut(BaseModel):
    entries: list[JournalEntry]
    dropped: int
    hours: int


class AgentMemoryOut(BaseModel):
    is_admin: bool
    admins_configured: bool
    lessons: dict[str, int] | None = None
    config: dict[str, Any]


class _Forbidden(Exception):
    pass


class _MemoryOff(Exception):
    pass


class MemoryChannel:
    """Mount with ``create_app(..., channels=[MemoryChannel(service)])``; share ``service`` with the provider."""

    def __init__(self, service: MemoryService | None = None, *, path: str = "/v1/memory",
                 agent_memory: Any = None) -> None:
        self.path = path.rstrip("/")
        self._service = service
        self._agent_memory = agent_memory

    def install(self, app: FastAPI, service: Any, settings: Any) -> None:
        if self._agent_memory is None and self._service is None:
            from .settings import agent_memory

            self._agent_memory = agent_memory(settings)
        if self._service is None:
            self._service = self._agent_memory.memory
        memory = self._service
        am = self._agent_memory if self._agent_memory is not None and self._agent_memory.memory is memory else None
        app.state.memory_service = memory
        app.state.agent_memory = am
        agent_name = settings.service_name

        def require(request: Request, profile: str) -> None:
            # Set by create_app once the agent is built; a hand-made app without it serves every route.
            profiles = getattr(request.app.state, "memory_profiles", None)
            if profiles is not None and profile not in profiles:
                raise _MemoryOff()

        def caller(request: Request) -> MemoryUser:
            require(request, "user")
            who = http_caller(request, settings, channel="memory")
            user_id = who.user_id or memory.dev_user
            if not user_id:
                raise _Unauthenticated()
            return MemoryUser(user_id=user_id, tenant_id=who.tenant_id)

        def admin(request: Request, profile: str = "agent") -> tuple[MemoryUser, str]:
            """A memory admin's identity and pseudonym, or 401/403/404."""
            if am is None:
                raise _MemoryOff()
            require(request, profile)
            who = http_caller(request, settings, channel="memory")
            user_id = who.user_id or memory.dev_user
            if not user_id:
                raise _Unauthenticated()
            if not (am.is_admin(user_id) or (getattr(settings, "approver_role", None) and who.is_approver)):
                raise _Forbidden()
            return MemoryUser(user_id=user_id, tenant_id=who.tenant_id), memory.pseudonym(user_id, who.tenant_id)

        if am is not None:
            app.include_router(self._agent_router(am, settings, require, admin))
            an = self._analytics(settings, memory, am)
            if an is not None:
                from .analytics.api import analytics_router

                app.state.analytics = an
                app.include_router(analytics_router(an, lambda request: admin(request, "analytics"), am))

        router = APIRouter(prefix=self.path, tags=["memory"], responses=_ERRORS)

        @router.get("", response_model=MemoryListOut)
        async def list_memory(request: Request) -> MemoryListOut:
            items = await memory.recall(caller(request))
            return MemoryListOut(items=[MemoryItemOut.of(i) for i in items], count=len(items),
                                 max_items=memory.policy.max_items, retention_days=memory.policy.ttl_days)

        @router.post("", response_model=MemoryItemOut, status_code=201,
                     responses={200: {"model": MemoryItemOut, "description": "Already remembered, or a replay"}})
        async def add_memory(
            body: MemoryItemIn,
            request: Request,
            response: Response,
            idempotency_key: str = Header(alias="Idempotency-Key", pattern=IDEMPOTENCY_KEY_PATTERN),
        ) -> MemoryItemOut:
            item, created = await memory.remember(caller(request), body.text, kind=body.kind, source="user",
                                                  idempotency_key=idempotency_key)
            if not created:
                response.status_code = 200
            return MemoryItemOut.of(item)

        @router.patch("/{item_id}", response_model=MemoryItemOut,
                      responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}})
        async def edit_memory(body: MemoryItemPatch, request: Request,
                              item_id: str = Path(pattern=ITEM_ID_PATTERN)) -> MemoryItemOut:
            return MemoryItemOut.of(await memory.edit(caller(request), item_id, body.text))

        @router.delete("/{item_id}", status_code=204, responses={409: {"model": ErrorBody}})
        async def delete_memory(request: Request, item_id: str = Path(pattern=ITEM_ID_PATTERN)) -> Response:
            await memory.delete(caller(request), item_id)
            return Response(status_code=204)

        @router.delete("", status_code=204)
        async def forget_everything(request: Request) -> Response:
            await memory.forget_all(caller(request))
            return Response(status_code=204)

        @router.get("/export", response_class=PlainTextResponse,
                    responses={200: {"content": {"text/markdown": {}}, "description": "Markdown export"}})
        async def export_memory(request: Request) -> PlainTextResponse:
            text = await memory.export_markdown(caller(request), agent_name=agent_name)
            return PlainTextResponse(text, media_type="text/markdown; charset=utf-8",
                                     headers={"Content-Disposition": 'attachment; filename="memory.md"',
                                              "Cache-Control": "no-store"})

        app.include_router(router)

        @app.exception_handler(_Unauthenticated)
        async def _unauthenticated(_: Request, __: _Unauthenticated) -> JSONResponse:
            return _error(401, "unauthenticated", "Sign in to see what this agent remembers about you.")

        @app.exception_handler(_Forbidden)
        async def _forbidden(_: Request, __: _Forbidden) -> JSONResponse:
            return _error(403, "not_memory_admin", "Only a memory admin can see or change the agent's own memory.")

        @app.exception_handler(_MemoryOff)
        async def _memory_off(_: Request, __: _MemoryOff) -> JSONResponse:
            return _error(404, "memory_off", "This agent doesn't keep that kind of memory.")

        @app.exception_handler(MemoryError_)
        async def _memory_error(_: Request, exc: MemoryError_) -> JSONResponse:
            headers = None
            if isinstance(exc, (MemoryRateLimited, MemoryUnavailable)):
                headers = {"Retry-After": str(exc.retry_after)}
            return _error(exc.status, exc.code, exc.message, headers)

    @staticmethod
    def _analytics(settings: Any, memory: MemoryService, am: Any) -> Any:
        """The agent's analytics (the same instance ``build_agent`` wired), or None when off or not installed."""
        try:
            from .analytics import analytics
        except ImportError:  # pragma: no cover - duckdb extra not installed
            return None
        try:
            return analytics(settings, memory=memory, agent_memory=am)
        except ValueError:
            return None

    def _agent_router(self, am: Any, settings: Any, require: Any, admin: Any) -> APIRouter:
        errors = {**_ERRORS, 403: {"model": ErrorBody, "description": "Not a memory admin"},
                  404: {"model": ErrorBody, "description": "No such item, or the agent keeps no such memory"}}
        router = APIRouter(prefix=self.path + "/agent", tags=["agent memory"], responses=errors)
        lessons, subjects, journal, memory = am.lessons, am.subjects, am.journal, am.memory

        @router.get("", response_model=AgentMemoryOut)
        async def summary(request: Request) -> AgentMemoryOut:
            require(request, "agent")
            who = http_caller(request, settings, channel="memory")
            user_id = who.user_id or memory.dev_user
            if not user_id:
                raise _Unauthenticated()
            is_admin = am.is_admin(user_id) or bool(getattr(settings, "approver_role", None) and who.is_approver)
            counts = None
            if is_admin:
                items = await lessons.list(who.tenant_id)
                counts = {s: sum(1 for i in items if i.status == s) for s in ("active", "pending", "archived")}
            return AgentMemoryOut(is_admin=is_admin, admins_configured=bool(am.admins), lessons=counts,
                                  config=am.config)

        @router.get("/lessons", response_model=LessonListOut)
        async def list_lessons(request: Request, status: LessonStatus | None = Query(default=None)) -> LessonListOut:
            user, _ = admin(request)
            items = await lessons.list(user.tenant_id)
            counts = {s: sum(1 for i in items if i.status == s) for s in ("active", "pending", "archived")}
            return LessonListOut(items=[i for i in items if status is None or i.status == status], counts=counts)

        @router.post("/lessons", response_model=Lesson, status_code=201,
                     responses={200: {"model": Lesson, "description": "Already known"}})
        async def add_lesson(body: LessonIn, request: Request, response: Response) -> Lesson:
            user, by = admin(request)
            lesson, created = await lessons.propose(body.text, origin="admin", by=by, tenant_id=user.tenant_id)
            if not created:
                response.status_code = 200
            return lesson

        @router.patch("/lessons/{lesson_id}", response_model=Lesson)
        async def edit_lesson(body: LessonIn, request: Request,
                              lesson_id: str = Path(pattern=LESSON_ID_PATTERN)) -> Lesson:
            user, by = admin(request)
            return await lessons.edit(lesson_id, body.text, by=by, tenant_id=user.tenant_id)

        def action(name: Literal["approve", "reject", "restore"]):
            async def handler(request: Request, lesson_id: str = Path(pattern=LESSON_ID_PATTERN)) -> Lesson:
                user, by = admin(request)
                return await getattr(lessons, name)(lesson_id, by=by, tenant_id=user.tenant_id)

            handler.__name__ = f"{name}_lesson"
            return handler

        for name in ("approve", "reject", "restore"):
            router.add_api_route(f"/lessons/{{lesson_id}}/{name}", action(name), methods=["POST"], response_model=Lesson)

        @router.post("/lessons/{lesson_id}/revert", response_model=Lesson)
        async def revert_lesson(body: RevertIn, request: Request,
                                lesson_id: str = Path(pattern=LESSON_ID_PATTERN)) -> Lesson:
            user, by = admin(request)
            return await lessons.revert(lesson_id, body.revision, by=by, tenant_id=user.tenant_id)

        @router.delete("/lessons/{lesson_id}", status_code=204)
        async def archive_lesson(request: Request, lesson_id: str = Path(pattern=LESSON_ID_PATTERN)) -> Response:
            user, by = admin(request)
            await lessons.archive(lesson_id, by=by, tenant_id=user.tenant_id)
            return Response(status_code=204)

        @router.get("/subjects", response_model=SubjectListOut)
        async def list_subjects(request: Request, limit: int = Query(default=100, ge=1, le=1000)) -> SubjectListOut:
            user, _ = admin(request)
            items = await subjects.recent(tenant_id=user.tenant_id, limit=limit)
            return SubjectListOut(items=[SubjectRef(subject=s, last_at=t) for s, t in items])

        @router.get("/subjects/{subject}", response_model=SubjectHistoryOut)
        async def subject_history(request: Request,
                                  subject: str = Path(pattern=SUBJECT_PATTERN)) -> SubjectHistoryOut:
            user, _ = admin(request)
            return SubjectHistoryOut(subject=subject, events=await subjects.history(subject, tenant_id=user.tenant_id))

        @router.delete("/subjects/{subject}", status_code=204)
        async def forget_subject(request: Request, subject: str = Path(pattern=SUBJECT_PATTERN)) -> Response:
            user, _ = admin(request)
            await subjects.forget(subject, tenant_id=user.tenant_id)
            return Response(status_code=204)

        @router.get("/journal", response_model=JournalOut)
        async def read_journal(request: Request, hours: int = Query(default=24, ge=1, le=168),
                               subject: str | None = Query(default=None, pattern=SUBJECT_PATTERN),
                               limit: int = Query(default=200, ge=1, le=1000)) -> JournalOut:
            admin(request)
            entries, dropped = await journal.recent(hours=hours, subject=subject, limit=limit)
            return JournalOut(entries=entries, dropped=dropped, hours=hours)

        return router


class _Unauthenticated(Exception):
    pass
