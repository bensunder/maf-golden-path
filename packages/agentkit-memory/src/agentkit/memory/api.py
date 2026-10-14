"""``MemoryChannel``: the versioned HTTP API for people to see and control what an agent remembers.

Every route acts on the caller's own memory only, identified the same way as chat (platform auth headers),
so there is no route that can read another person's memory.

    GET    /v1/memory              200 MemoryListOut
    POST   /v1/memory              201 created | 200 already remembered or replayed   (Idempotency-Key required)
    PATCH  /v1/memory/{id}         200 MemoryItemOut
    DELETE /v1/memory/{id}         204 (idempotent)
    DELETE /v1/memory              204 forget everything
    GET    /v1/memory/export       200 text/markdown

Errors: ``{"detail": {"code": ..., "message": ...}}`` with 401, 404, 409, 422, 429 (Retry-After) or
503 (Retry-After). POST bodies must be JSON (the host's CSRF rule); PATCH and DELETE can't be sent
cross-site without a CORS preflight.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Header, Path, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from agentkit.hosting.app import http_caller

from .errors import MemoryError_, MemoryRateLimited, MemoryUnavailable
from .models import IDEMPOTENCY_KEY_PATTERN, ITEM_ID_PATTERN, MemoryItemIn, MemoryItemOut, MemoryItemPatch, MemoryListOut
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


class MemoryChannel:
    """Mount with ``create_app(..., channels=[MemoryChannel(service)])``; share ``service`` with the provider."""

    def __init__(self, service: MemoryService | None = None, *, path: str = "/v1/memory") -> None:
        self.path = path.rstrip("/")
        self._service = service

    def install(self, app: FastAPI, service: Any, settings: Any) -> None:
        if self._service is None:
            from .settings import memory_service

            self._service = memory_service(settings)
        memory = self._service
        app.state.memory_service = memory
        agent_name = settings.service_name

        def caller(request: Request) -> MemoryUser:
            who = http_caller(request, settings, channel="memory")
            user_id = who.user_id or memory.dev_user
            if not user_id:
                raise _Unauthenticated()
            return MemoryUser(user_id=user_id, tenant_id=who.tenant_id)

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

        @app.exception_handler(MemoryError_)
        async def _memory_error(_: Request, exc: MemoryError_) -> JSONResponse:
            headers = None
            if isinstance(exc, (MemoryRateLimited, MemoryUnavailable)):
                headers = {"Retry-After": str(exc.retry_after)}
            return _error(exc.status, exc.code, exc.message, headers)


class _Unauthenticated(Exception):
    pass
