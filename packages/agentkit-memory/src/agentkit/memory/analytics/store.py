"""``AnalyticsStore``: an agent's in-process DuckDB database for its audit trail and semantic memory.

Two tables (docs/analytics.md):

* ``agent_episodes``: one row per agent run: who/what started it, timing, outcome, model and token use, tool
  calls with their latency, and (when capture is on) the run's input, prompt messages and output **after PII
  redaction**. Never raw text.
* ``semantic_memory``: searchable copies of what the agent remembers (lessons, subject events, each user's
  notes), with a vector embedding per row (``FLOAT[n]``) for hybrid SQL + vector search.

Concurrency, because DuckDB allows one writer per database file:

* **One writer.** A single background thread owns the read-write connection and applies queued writes in
  batches, each batch in one transaction. Callers never wait on disk: ``submit`` is a non-blocking put on a
  bounded queue; when it is full the write is dropped and counted rather than slowing a reply.
* **Many readers.** Reads borrow one of a pool of cursors (DuckDB's thread-safe duplicate connections) and run
  under MVCC beside the writer. Ad-hoc SQL is checked to be a single SELECT first (``sqlguard``) and is
  interrupted at its timeout.
* **One process per file.** Other processes (dashboards, other replicas) read the Parquet exports instead; with
  several replicas each keeps its own file and exports under its own ``replica=`` prefix.

Retention: every row carries ``expires_at`` and expired rows are deleted hourly. Exports: new episodes and a
snapshot of semantic memory are written as Parquet to a local folder or S3 on a schedule.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import socket
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from opentelemetry import metrics

from .embeddings import Embedder, EmbedError
from .sqlguard import check_select

__all__ = ["AnalyticsStore", "Episode", "MemoryRecord", "SearchHit", "ToolCallRecord"]

logger = logging.getLogger(__name__)
_ops = metrics.get_meter("agentkit.memory").create_counter(
    "agentkit.analytics.operations", unit="{operation}", description="Analytics store operations by outcome")

SCHEMA_VERSION = 1
_DAY = 86_400.0
_WORD = re.compile(r"[a-z0-9]{3,}")
TABLES = ("agent_episodes", "semantic_memory")


def _ts(epoch: float | None) -> datetime | None:
    """Times are stored as UTC ``TIMESTAMP`` (no zone), which reads back without extra Python packages."""
    return None if epoch is None else datetime.fromtimestamp(epoch, tz=UTC).replace(tzinfo=None)


def _epoch(value: datetime | None) -> float | None:
    return None if value is None else value.replace(tzinfo=UTC).timestamp()


@dataclass
class ToolCallRecord:
    name: str
    ok: bool = True
    duration_ms: int = 0
    arguments_redacted: str | None = None
    result_chars: int = 0


@dataclass
class Episode:
    run_id: str
    agent: str
    started_at: float
    duration_ms: int = 0
    trigger: str = "system"
    actor: str = ""
    tenant: str | None = None
    session: str | None = None
    outcome: str = "ok"
    blocked_reason: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    capture: str = "off"  # "off" or "redacted": whether the text columns below are filled
    input_redacted: str | None = None
    prompt_redacted: list[dict[str, str]] | None = None
    output_redacted: str | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)


@dataclass
class MemoryRecord:
    memory_id: str
    agent: str
    kind: str  # lesson | subject_event | user_note
    text: str
    tenant: str | None = None
    scope: str = ""  # "" for the whole agent, or a user's pseudonym for their own notes
    subject: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float | None = None


@dataclass
class SearchHit:
    memory_id: str
    kind: str
    text: str
    subject: str | None
    score: float
    vector_score: float | None
    lexical_score: float
    metadata: dict[str, Any]


class AnalyticsStore:
    def __init__(
        self,
        path: str,
        *,
        agent: str,
        embedder: Embedder | None = None,
        retention_days: int = 30,
        memory_limit: str = "256MB",
        threads: int = 2,
        queue_size: int = 10_000,
        readers: int = 4,
        export_uri: str | None = None,
        export_interval_seconds: float = 3600.0,
        filesystem: Any = None,
        replica: str | None = None,
        clock: Callable[[], float] = time.time,
        batch_wait: float = 0.2,
    ) -> None:
        if not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        self.path = path
        self.agent = agent
        self.embedder = embedder
        self.retention_days = retention_days
        self.export_uri = (export_uri or "").rstrip("/") or None
        self.export_interval = export_interval_seconds
        self.replica = re.sub(r"[^A-Za-z0-9_.-]", "-", replica or socket.gethostname())[:64]
        self._config = {"memory_limit": memory_limit, "threads": int(threads)}
        self._filesystem = filesystem
        self._clock = clock
        self._batch_wait = batch_wait
        self._queue: queue.Queue[Callable[[Any], None] | None] = queue.Queue(maxsize=queue_size)
        self._readers: queue.Queue[Any] = queue.Queue()
        self._reader_count = max(1, readers)
        self._con: Any = None
        self._writer: threading.Thread | None = None
        self._embedding_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._dims: int | None = None
        self._last_purge = 0.0
        self._last_export = 0.0
        self.dropped = 0
        self.failed_writes = 0
        self.last_export_error: str | None = None
        self.last_export_at: float | None = None

    # ---------------------------------------------------------------------------- lifecycle
    def start(self) -> AnalyticsStore:
        import duckdb

        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(self.path, config=self._config)
        if self._filesystem is not None:
            self._con.register_filesystem(self._filesystem)
        self._migrate()
        for _ in range(self._reader_count):
            self._readers.put(self._con.cursor())
        self._last_export = self._clock()
        self._writer = threading.Thread(target=self._write_loop, name="agentkit-analytics-writer", daemon=True)
        self._writer.start()
        if self.embedder is not None:
            self._embedding_thread = threading.Thread(target=self._embed_loop, name="agentkit-analytics-embed",
                                                      daemon=True)
            self._embedding_thread.start()
        return self

    def close(self, timeout: float = 10.0) -> None:
        if self._con is None:
            return
        self.flush(timeout)
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        for thread in (self._writer, self._embedding_thread):
            if thread is not None:
                thread.join(timeout)
        while not self._readers.empty():
            self._readers.get_nowait().close()
        self._con.close()
        self._con = None

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until every queued write is applied (tests, shutdown). True when drained in time."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._queue.unfinished_tasks

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ------------------------------------------------------------------------------- schema
    def _migrate(self) -> None:
        c = self._con
        c.execute("CREATE TABLE IF NOT EXISTS analytics_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
        c.execute("""CREATE TABLE IF NOT EXISTS agent_episodes (
            episode_id VARCHAR PRIMARY KEY, run_id VARCHAR NOT NULL, agent VARCHAR NOT NULL, tenant VARCHAR,
            session VARCHAR, actor VARCHAR, trigger VARCHAR, started_at TIMESTAMP NOT NULL, duration_ms INTEGER,
            outcome VARCHAR, blocked_reason VARCHAR, model VARCHAR, input_tokens BIGINT, output_tokens BIGINT,
            capture VARCHAR, input_redacted VARCHAR, prompt_redacted JSON, output_redacted VARCHAR,
            tool_calls JSON, tool_count INTEGER, ingested_at TIMESTAMP NOT NULL, expires_at TIMESTAMP NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS semantic_memory (
            memory_id VARCHAR PRIMARY KEY, agent VARCHAR NOT NULL, tenant VARCHAR, kind VARCHAR NOT NULL,
            scope VARCHAR NOT NULL, subject VARCHAR, text VARCHAR NOT NULL, metadata JSON, embed_model VARCHAR,
            created_at TIMESTAMP, updated_at TIMESTAMP, expires_at TIMESTAMP)""")
        c.execute("CREATE INDEX IF NOT EXISTS episodes_started ON agent_episodes (started_at)")
        c.execute("CREATE INDEX IF NOT EXISTS memory_scope ON semantic_memory (agent, kind, scope)")
        c.execute("INSERT OR REPLACE INTO analytics_meta VALUES ('schema_version', ?)", [str(SCHEMA_VERSION)])
        columns = {r[0]: r[1] for r in c.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'semantic_memory'"
        ).fetchall()}
        if self.embedder is not None:
            dims = int(self.embedder.dimensions)
            current = columns.get("embedding")
            if current != f"FLOAT[{dims}]":
                c.execute("DROP INDEX IF EXISTS memory_scope")  # DuckDB won't alter a table with an index
                if current is not None:  # the model changed: old vectors are meaningless, re-embed everything
                    c.execute("ALTER TABLE semantic_memory DROP COLUMN embedding")
                c.execute(f"ALTER TABLE semantic_memory ADD COLUMN embedding FLOAT[{dims}]")
                c.execute("UPDATE semantic_memory SET embed_model = NULL")
                c.execute("CREATE INDEX IF NOT EXISTS memory_scope ON semantic_memory (agent, kind, scope)")
            self._dims = dims

    # ------------------------------------------------------------------------------- writes
    def submit(self, op: Callable[[Any], None]) -> bool:
        """Queue a write (a function of the writer connection). False when the queue is full and it was dropped."""
        if self._con is None:
            return False
        try:
            self._queue.put_nowait(op)
            return True
        except queue.Full:
            self.dropped += 1
            _ops.add(1, {"op": "write", "outcome": "dropped"})
            return False

    def record_episode(self, episode: Episode) -> bool:
        now = self._clock()
        captured = episode.capture == "redacted"
        row = [
            uuid.uuid4().hex, episode.run_id, episode.agent, episode.tenant, episode.session, episode.actor,
            episode.trigger, _ts(episode.started_at), episode.duration_ms, episode.outcome, episode.blocked_reason,
            episode.model, episode.input_tokens, episode.output_tokens, "redacted" if captured else "off",
            episode.input_redacted if captured else None,
            json.dumps(episode.prompt_redacted) if captured and episode.prompt_redacted is not None else None,
            episode.output_redacted if captured else None,
            json.dumps([tc.__dict__ for tc in episode.tool_calls]), len(episode.tool_calls),
            _ts(now), _ts(now + self.retention_days * _DAY),
        ]
        return self.submit(lambda c: c.execute(
            "INSERT INTO agent_episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row))

    def upsert_memory(self, record: MemoryRecord) -> bool:
        now = self._clock()
        row = [record.memory_id, record.agent, record.tenant, record.kind, record.scope, record.subject, record.text,
               json.dumps(record.metadata), _ts(record.created_at or now), _ts(record.updated_at or now),
               _ts(record.expires_at)]

        def op(c: Any) -> None:
            same = c.execute("SELECT text FROM semantic_memory WHERE memory_id = ?", [record.memory_id]).fetchone()
            if same is not None and same[0] == record.text:  # same text: keep its embedding, refresh the rest
                c.execute("UPDATE semantic_memory SET metadata = ?, updated_at = ?, expires_at = ?, subject = ? "
                          "WHERE memory_id = ?", [row[7], row[9], row[10], record.subject, record.memory_id])
                return
            c.execute("DELETE FROM semantic_memory WHERE memory_id = ?", [record.memory_id])
            c.execute("INSERT INTO semantic_memory (memory_id, agent, tenant, kind, scope, subject, text, metadata, "
                      "created_at, updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)

        return self.submit(op)

    def replace_memories(self, prefix: str, records: Sequence[MemoryRecord]) -> bool:
        """Make the rows whose id starts with ``prefix`` exactly ``records`` (one source document's contents),
        in one transaction. Rows whose text didn't change keep their embedding."""
        now = self._clock()
        rows = {r.memory_id: [r.memory_id, r.agent, r.tenant, r.kind, r.scope, r.subject, r.text,
                              json.dumps(r.metadata), _ts(r.created_at or now), _ts(r.updated_at or now),
                              _ts(r.expires_at)] for r in records if r.memory_id.startswith(prefix)}

        def op(c: Any) -> None:
            existing = dict(c.execute("SELECT memory_id, text FROM semantic_memory WHERE starts_with(memory_id, ?)",
                                      [prefix]).fetchall())
            gone = [mid for mid in existing if mid not in rows]
            if gone:
                c.execute("DELETE FROM semantic_memory WHERE list_contains(?, memory_id)", [gone])
            for mid, row in rows.items():
                if existing.get(mid) == row[6]:
                    c.execute("UPDATE semantic_memory SET metadata = ?, updated_at = ?, expires_at = ?, subject = ?, "
                              "scope = ? WHERE memory_id = ?", [row[7], row[9], row[10], row[5], row[4], mid])
                    continue
                if mid in existing:
                    c.execute("DELETE FROM semantic_memory WHERE memory_id = ?", [mid])
                c.execute("INSERT INTO semantic_memory (memory_id, agent, tenant, kind, scope, subject, text, "
                          "metadata, created_at, updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          row)

        return self.submit(op)

    def delete_memory(self, memory_id: str | None = None, *, prefix: str | None = None) -> bool:
        if memory_id:
            return self.submit(lambda c: c.execute("DELETE FROM semantic_memory WHERE memory_id = ?", [memory_id]))
        if prefix:
            return self.submit(lambda c: c.execute("DELETE FROM semantic_memory WHERE starts_with(memory_id, ?)",
                                                   [prefix]))
        return False

    def _write_loop(self) -> None:
        while True:
            batch: list[Callable[[Any], None]] = []
            try:
                first = self._queue.get(timeout=1.0)
            except queue.Empty:
                first = ...
            if first is None:
                self._queue.task_done()
                return
            if first is not ...:
                batch.append(first)
                deadline = time.monotonic() + self._batch_wait
                while len(batch) < 500:
                    try:
                        item = self._queue.get(timeout=max(0.0, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    if item is None:
                        self._queue.task_done()
                        self._apply(batch)
                        return
                    batch.append(item)
                self._apply(batch)
            self._housekeeping()
            if self._stop.is_set() and self._queue.empty():
                return

    def _apply(self, batch: list[Callable[[Any], None]]) -> None:
        c = self._con
        try:
            c.execute("BEGIN TRANSACTION")
            for op in batch:
                op(c)
            c.execute("COMMIT")
            _ops.add(len(batch), {"op": "write", "outcome": "ok"})
        except Exception as exc:
            c.execute("ROLLBACK")
            logger.warning("analytics batch failed (%s); applying one by one", type(exc).__name__)
            for op in batch:  # one bad row must not lose the rest
                try:
                    op(c)
                    _ops.add(1, {"op": "write", "outcome": "ok"})
                except Exception as single:
                    self.failed_writes += 1
                    _ops.add(1, {"op": "write", "outcome": "error"})
                    logger.warning("analytics write dropped: %s", type(single).__name__)
        finally:
            for _ in batch:
                self._queue.task_done()

    def _housekeeping(self) -> None:
        now = self._clock()
        if now - self._last_purge >= 3600:
            self._last_purge = now
            try:
                cutoff = _ts(now)
                self._con.execute("DELETE FROM agent_episodes WHERE expires_at <= ?", [cutoff])
                self._con.execute("DELETE FROM semantic_memory WHERE expires_at IS NOT NULL AND expires_at <= ?",
                                  [cutoff])
            except Exception as exc:
                logger.warning("analytics purge failed: %s", type(exc).__name__)
        if self.export_uri and now - self._last_export >= self.export_interval:
            self._last_export = now
            self._export(now)

    # ------------------------------------------------------------------------------ exports
    def export_now(self) -> None:
        """Queue an export of new episodes and a semantic-memory snapshot (also runs on its schedule)."""
        if self.export_uri:
            self.submit(lambda _c: self._export(self._clock()))

    def _export(self, now: float) -> None:
        c, base = self._con, self.export_uri
        try:
            mark = c.execute("SELECT value FROM analytics_meta WHERE key = 'export_watermark'").fetchone()
            since = _ts(float(mark[0])) if mark else _ts(0)
            until = _ts(now)
            days = c.execute("SELECT DISTINCT strftime(started_at, '%Y-%m-%d') FROM agent_episodes "
                             "WHERE ingested_at > ? AND ingested_at <= ?", [since, until]).fetchall()
            stamp = int(now * 1000)
            for (day,) in days:
                folder = f"{base}/agent_episodes/agent={self.agent}/date={day}"
                self._ensure_dir(folder)
                c.execute(f"COPY (SELECT * FROM agent_episodes WHERE ingested_at > ? AND ingested_at <= ? AND "
                          f"strftime(started_at, '%Y-%m-%d') = ?) TO "
                          f"'{folder}/{self.replica}-{stamp}.parquet' (FORMAT parquet)", [since, until, day])
            folder = f"{base}/semantic_memory/agent={self.agent}"
            self._ensure_dir(folder)
            c.execute(f"COPY (SELECT * FROM semantic_memory) TO '{folder}/{self.replica}-latest.parquet' "
                      "(FORMAT parquet)")
            c.execute("INSERT OR REPLACE INTO analytics_meta VALUES ('export_watermark', ?)", [repr(now)])
            self.last_export_at, self.last_export_error = now, None
            _ops.add(1, {"op": "export", "outcome": "ok"})
        except Exception as exc:
            self.last_export_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            _ops.add(1, {"op": "export", "outcome": "error"})
            logger.warning("analytics export failed: %s", self.last_export_error)

    def _ensure_dir(self, folder: str) -> None:
        if "://" not in folder:
            Path(folder).mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------------------- embedding
    def _embed_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                rows = self._read(lambda cur: cur.execute(
                    "SELECT memory_id, text FROM semantic_memory WHERE embed_model IS NULL "
                    "ORDER BY updated_at LIMIT 32").fetchall())
            except Exception:
                rows = []
            if not rows:
                self._stop.wait(1.0)
                continue
            try:
                vectors = self.embedder.embed([r[1] for r in rows])
                backoff = 1.0
            except EmbedError as exc:
                logger.warning("embedding paused: %s", exc)
                self._stop.wait(min(backoff, 60.0))
                backoff *= 2
                continue
            model, dims = self.embedder.model, self._dims
            pairs = [(r[0], r[1], v) for r, v in zip(rows, vectors) if len(v) == dims]

            def op(c: Any, pairs=pairs, model=model, dims=dims) -> None:
                for memory_id, text, vector in pairs:  # only if the text didn't change meanwhile
                    c.execute(f"UPDATE semantic_memory SET embedding = ?::FLOAT[{dims}], embed_model = ? "
                              "WHERE memory_id = ? AND text = ?", [vector, model, memory_id, text])

            self.submit(op)
            self.flush(5.0)

    # -------------------------------------------------------------------------------- reads
    def _read(self, fn: Callable[[Any], Any], timeout: float | None = None) -> Any:
        cursor = self._readers.get(timeout=30)
        timer = threading.Timer(timeout, cursor.interrupt) if timeout else None
        try:
            if timer:
                timer.start()
            return fn(cursor)
        finally:
            if timer:
                timer.cancel()
            self._readers.put(cursor)

    async def read(self, fn: Callable[[Any], Any], timeout: float | None = None) -> Any:
        return await asyncio.to_thread(self._read, fn, timeout)

    async def episodes(self, *, hours: float = 24, outcome: str | None = None, limit: int = 100,
                       tenant: str | None = None) -> list[dict[str, Any]]:
        since = _ts(self._clock() - hours * 3600)
        sql = ("SELECT episode_id, run_id, agent, trigger, actor, started_at, duration_ms, outcome, blocked_reason, "
               "model, input_tokens, output_tokens, tool_count, tool_calls, capture, input_redacted, "
               "output_redacted FROM agent_episodes WHERE started_at >= ? AND agent = ?")
        params: list[Any] = [since, self.agent]
        if outcome:
            sql += " AND outcome = ?"
            params.append(outcome)
        if tenant is not None:
            sql += " AND tenant IS NOT DISTINCT FROM ?"
            params.append(tenant)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))

        def run(cur: Any) -> list[dict[str, Any]]:
            res = cur.execute(sql, params)
            cols = [d[0] for d in res.description]
            out = []
            for row in res.fetchall():
                item = dict(zip(cols, row))
                item["started_at"] = _epoch(item["started_at"])
                item["tool_calls"] = json.loads(item["tool_calls"] or "[]")
                out.append(item)
            return out

        return await self.read(run)

    async def search(self, text: str, *, tenant: str | None = None, scopes: Sequence[str] = ("",),
                     kinds: Sequence[str] | None = None, subject: str | None = None, k: int = 8,
                     embed_timeout: float = 2.0) -> list[SearchHit]:
        """Hybrid search: SQL filters (agent, tenant, scope, kind, subject, not expired), then a score that is a
        blend of vector similarity (when there is an embedder) and word overlap."""
        words = sorted(set(_WORD.findall((text or "").lower())))[:12]
        vector: list[float] | None = None
        if self.embedder is not None and self._dims:
            try:
                vector = (await asyncio.wait_for(asyncio.to_thread(self.embedder.embed, [text]), embed_timeout))[0]
            except (TimeoutError, EmbedError):
                vector = None
        where = ["agent = ?", "tenant IS NOT DISTINCT FROM ?", f"scope IN ({', '.join('?' for _ in scopes)})",
                 "(expires_at IS NULL OR expires_at > ?)"]
        params: list[Any] = [self.agent, tenant, *scopes, _ts(self._clock())]
        if kinds:
            where.append(f"kind IN ({', '.join('?' for _ in kinds)})")
            params.extend(kinds)
        if subject:
            where.append("subject = ?")
            params.append(subject)
        lexical = " + ".join("CASE WHEN contains(lower(text), ?) THEN 1 ELSE 0 END" for _ in words) or "0"
        lex_params = list(words)
        lex_expr = f"(({lexical}) / {max(1, len(words))}.0)"
        if vector is not None:
            vec_expr = f"array_cosine_similarity(embedding, ?::FLOAT[{self._dims}])"
            score = f"(0.7 * COALESCE({vec_expr}, 0) + 0.3 * {lex_expr})"
            select = (f"SELECT memory_id, kind, text, subject, metadata, {vec_expr} AS vs, {lex_expr} AS ls, "
                      f"{score} AS score")
            sel_params = [vector, *lex_params, vector, *lex_params]
        else:
            select = f"SELECT memory_id, kind, text, subject, metadata, NULL AS vs, {lex_expr} AS ls, {lex_expr} AS score"
            sel_params = [*lex_params, *lex_params]
        sql = (f"{select} FROM semantic_memory WHERE {' AND '.join(where)} "
               f"ORDER BY score DESC, updated_at DESC LIMIT ?")
        all_params = [*sel_params, *params, max(1, min(int(k), 50))]

        def run(cur: Any) -> list[SearchHit]:
            rows = cur.execute(sql, all_params).fetchall()
            return [SearchHit(memory_id=r[0], kind=r[1], text=r[2], subject=r[3], metadata=json.loads(r[4] or "{}"),
                              vector_score=None if r[5] is None else float(r[5]), lexical_score=float(r[6]),
                              score=float(r[7] or 0)) for r in rows if (r[7] or 0) > 0]

        return await self.read(run)

    async def query(self, sql: str, *, max_rows: int = 200, timeout: float = 10.0,
                    tables: Sequence[str] = TABLES) -> dict[str, Any]:
        """A read-only SELECT over this agent's tables (memory admins). Checked by ``sqlguard`` first."""
        def run(cur: Any) -> dict[str, Any]:
            text = check_select(cur, sql, tables)
            res = cur.execute(f"SELECT * FROM ({text}) AS q LIMIT {int(max_rows) + 1}")
            cols = [d[0] for d in res.description]
            rows = res.fetchall()
            return {"columns": cols, "rows": [[_jsonable(v) for v in r] for r in rows[:max_rows]],
                    "truncated": len(rows) > max_rows}

        return await self.read(run, timeout)

    def stats(self) -> dict[str, Any]:
        def run(cur: Any) -> dict[str, Any]:
            episodes = cur.execute("SELECT count(*) FROM agent_episodes").fetchone()[0]
            memories, embedded = cur.execute(
                "SELECT count(*), count(embed_model) FROM semantic_memory").fetchone()
            return {"episodes": episodes, "memories": memories, "embedded": embedded}

        out = self._read(run) if self._con is not None else {}
        return {**out, "queue_depth": self.queue_depth, "dropped": self.dropped, "failed_writes": self.failed_writes,
                "embedder": getattr(self.embedder, "model", None), "dimensions": self._dims,
                "export_uri": _redact_uri(self.export_uri), "last_export_at": self.last_export_at,
                "last_export_error": self.last_export_error, "retention_days": self.retention_days}


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _redact_uri(uri: str | None) -> str | None:
    return re.sub(r"//[^/@]+@", "//***@", uri) if uri else uri
