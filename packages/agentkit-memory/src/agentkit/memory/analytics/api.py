"""``/v1/analytics``: the analytics store for memory admins (mounted by ``MemoryChannel`` when analytics is on).

    GET  /v1/analytics                    200 AnalyticsStatus      counts, queue, exports, embedder, datasets
    GET  /v1/analytics/episodes           200 EpisodeList          ?hours=24&outcome=&limit=100
    POST /v1/analytics/search             200 SearchOut            hybrid search over semantic memory
    POST /v1/analytics/query              200 QueryOut             one SELECT over the store or the datasets
    POST /v1/analytics/export             202                      write Parquet exports now
    POST /v1/analytics/reindex            202                      rebuild semantic memory from the memory store

Every route needs a memory admin (401 / 403 ``not_memory_admin``). SQL that isn't a single read-only SELECT
over the allowed tables answers ``422 sql_rejected``; a slow query ``504 query_timeout``; too many at once
``429 query_busy``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .indexer import reindex as rebuild_index
from .sqlguard import SqlRejected
from .sqltool import QueryFailed

__all__ = ["analytics_router"]

logger = logging.getLogger(__name__)
_TASKS: set[asyncio.Task[None]] = set()


class AnalyticsStatus(BaseModel):
    agent: str
    capture: str
    episodes: int
    memories: int
    embedded: int
    queue_depth: int
    dropped: int
    failed_writes: int
    embedder: str | None
    dimensions: int | None
    retention_days: int
    export_uri: str | None
    last_export_at: float | None
    last_export_error: str | None
    datasets: dict[str, Any]


class ToolCallOut(BaseModel):
    name: str
    ok: bool
    duration_ms: int
    arguments_redacted: str | None = None
    result_chars: int = 0


class EpisodeOut(BaseModel):
    episode_id: str
    run_id: str
    trigger: str | None
    actor: str | None
    started_at: float
    duration_ms: int | None
    outcome: str | None
    blocked_reason: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    tool_count: int
    tool_calls: list[ToolCallOut]
    capture: str | None
    input_redacted: str | None
    output_redacted: str | None


class EpisodeList(BaseModel):
    episodes: list[EpisodeOut]


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=300)
    kind: Literal["lesson", "subject_event"] | None = None
    subject: str | None = Field(default=None, max_length=120)
    k: int = Field(default=10, ge=1, le=50)


class SearchHitOut(BaseModel):
    memory_id: str
    kind: str
    text: str
    subject: str | None
    score: float
    vector_score: float | None
    lexical_score: float


class SearchOut(BaseModel):
    hits: list[SearchHitOut]


class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=6, max_length=8000)
    #: ``store``: this agent's agent_episodes and semantic_memory. ``datasets``: the configured Parquet datasets.
    target: Literal["store", "datasets"] = "store"
    max_rows: int = Field(default=200, ge=1, le=5000)


class QueryOut(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)


def analytics_router(an: Any, admin: Any, agent_memory: Any = None, *, path: str = "/v1/analytics") -> APIRouter:
    router = APIRouter(prefix=path, tags=["analytics"])
    store = an.store

    @router.get("", response_model=AnalyticsStatus)
    async def status(request: Request) -> AnalyticsStatus:
        admin(request)
        stats = await asyncio.to_thread(store.stats)
        datasets = an.catalog.summary() if an.catalog else {}
        return AnalyticsStatus(agent=store.agent, capture=an.settings.capture, datasets=datasets,
                               **{k: stats.get(k) for k in AnalyticsStatus.model_fields if k in stats})

    @router.get("/episodes", response_model=EpisodeList)
    async def episodes(request: Request, hours: int = Query(default=24, ge=1, le=24 * 90),
                       outcome: Literal["ok", "error", "blocked", "awaiting_approval"] | None = None,
                       limit: int = Query(default=100, ge=1, le=1000)) -> EpisodeList:
        user, _ = admin(request)
        rows = await store.episodes(hours=hours, outcome=outcome, limit=limit, tenant=user.tenant_id)
        return EpisodeList(episodes=[EpisodeOut(**{k: r.get(k) for k in EpisodeOut.model_fields}) for r in rows])

    @router.post("/search", response_model=SearchOut)
    async def search(body: SearchIn, request: Request) -> SearchOut:
        user, _ = admin(request)  # agent-wide memory only: admins don't search people's own notes here
        hits = await store.search(body.query, tenant=user.tenant_id, scopes=("",),
                                  kinds=[body.kind] if body.kind else ["lesson", "subject_event"],
                                  subject=body.subject, k=body.k)
        return SearchOut(hits=[SearchHitOut(memory_id=h.memory_id, kind=h.kind, text=h.text, subject=h.subject,
                                            score=h.score, vector_score=h.vector_score,
                                            lexical_score=h.lexical_score) for h in hits])

    @router.post("/query", response_model=QueryOut, responses={422: {}, 429: {}, 504: {}})
    async def query(body: QueryIn, request: Request) -> Any:
        admin(request)
        try:
            if body.target == "datasets":
                if an.catalog is None:
                    return _error(404, "no_datasets", "No datasets are configured (AGENTKIT_ANALYTICS_DATASETS).")
                result = await an.catalog.query(body.sql)
            else:
                result = await store.query(body.sql, max_rows=body.max_rows, timeout=an.settings.query_timeout_seconds)
        except SqlRejected as exc:
            return _error(422, "sql_rejected", f"{exc.reason}: {exc.message}")
        except QueryFailed as exc:
            status_code = {"timeout": 504, "busy": 429}.get(exc.reason, 422)
            return _error(status_code, f"query_{exc.reason}", exc.message)
        except Exception as exc:
            if "interrupt" in str(exc).lower():
                return _error(504, "query_timeout", "The query took too long.")
            return _error(422, "query_error", f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}")
        return QueryOut(columns=result["columns"], rows=result["rows"], truncated=result["truncated"])

    @router.post("/export", status_code=202)
    async def export(request: Request) -> Response:
        admin(request)
        if not store.export_uri:
            return _error(404, "no_export", "No export location is configured (AGENTKIT_ANALYTICS_EXPORT_URI).")
        store.export_now()
        return Response(status_code=202)

    @router.post("/reindex", status_code=202)
    async def reindex(request: Request) -> Response:
        user, _ = admin(request)
        if agent_memory is None:
            return _error(404, "memory_off", "This agent has no lessons or subject memory to index.")
        task = asyncio.get_running_loop().create_task(rebuild_index(agent_memory, user.tenant_id))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return Response(status_code=202)

    return router
