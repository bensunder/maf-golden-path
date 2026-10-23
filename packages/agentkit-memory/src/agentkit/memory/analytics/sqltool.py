"""``DatasetCatalog``: read-only SQL over Parquet datasets (local folders or S3), for agents and memory admins.

Operators list datasets as ``name=uri`` pairs (``AGENTKIT_ANALYTICS_DATASETS``), e.g.
``sales=s3://acme-lake/sales/*.parquet,episodes=/data/analytics/export/agent_episodes/**/*.parquet``.
Each becomes a view in a private in-memory DuckDB database; the agent sees only those names.

Two walls around every query:

1. ``sqlguard.check_select``: one SELECT, only the listed views, no table functions, no functions that read
   files, settings or the environment.
2. The connection itself: after the views are created it may read only the datasets' own directories
   (``allowed_directories``), all other file and network access is off (``enable_external_access=false``), and
   the configuration is locked so SQL can't turn any of that back on. It has its own memory and thread limits.

Then: a row cap, a timeout (the query is interrupted), at most a few queries at once, and string results are
PII-redacted before they reach the model. A dataset that can't be read (missing bucket, bad credentials) is left
out with its error shown in ``describe()``, rather than failing every query.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import Mapping
from typing import Any

from opentelemetry import metrics

from .episodes import redact
from .sqlguard import SqlRejected, check_select
from .store import _jsonable

__all__ = ["DatasetCatalog", "QueryFailed", "parse_datasets"]

logger = logging.getLogger(__name__)
_ops = metrics.get_meter("agentkit.memory").create_counter(
    "agentkit.analytics.queries", unit="{query}", description="Dataset SQL queries by outcome")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_RETRY_SECONDS = 60.0


class QueryFailed(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason, self.message = reason, message


def parse_datasets(spec: str | Mapping[str, str]) -> dict[str, str]:
    """``"a=s3://b/x/*.parquet,c=/data/y"`` -> ``{"a": ..., "c": ...}``; names are lowercase identifiers."""
    pairs = spec.items() if isinstance(spec, Mapping) else (
        part.split("=", 1) for part in (p.strip() for p in spec.split(",")) if part)
    out: dict[str, str] = {}
    for pair in pairs:
        if len(pair) != 2 or not pair[1].strip():
            raise ValueError(f"datasets are name=uri pairs: {'='.join(pair)!r}")
        name, uri = pair[0].strip().lower(), pair[1].strip()
        if not _NAME.match(name):
            raise ValueError(f"dataset name {name!r} must be a lowercase identifier")
        if not (uri.startswith(("s3://", "/")) and uri.endswith((".parquet", "/", "*"))):
            raise ValueError(f"dataset {name!r}: use an s3:// or absolute path to Parquet files")
        out[name] = uri.rstrip("/") + "/**/*.parquet" if uri.endswith("/") else uri
    return out


def _directory(uri: str) -> str:
    """The folder a dataset may be read from: everything before the first glob character."""
    head = re.split(r"[*?\[{]", uri, maxsplit=1)[0]
    return head if head.endswith("/") else head.rsplit("/", 1)[0] + "/"


class DatasetCatalog:
    def __init__(self, datasets: Mapping[str, str] | str, *, filesystem: Any = None, max_rows: int = 200,
                 timeout_seconds: float = 10.0, memory_limit: str = "512MB", threads: int = 2,
                 concurrency: int = 2, max_cell_chars: int = 500) -> None:
        self.datasets = parse_datasets(datasets)
        self.max_rows = max_rows
        self.timeout = timeout_seconds
        self.max_cell_chars = max_cell_chars
        self._filesystem = filesystem
        self._config = {"memory_limit": memory_limit, "threads": int(threads)}
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, concurrency))
        self._con: Any = None
        self._built_at = 0.0
        self.errors: dict[str, str] = {}
        self.columns: dict[str, list[tuple[str, str]]] = {}

    @property
    def names(self) -> list[str]:
        return sorted(self.datasets)

    # ------------------------------------------------------------------------------- set-up
    def _connection(self) -> Any:
        with self._lock:
            stale = self.errors and time.monotonic() - self._built_at > _RETRY_SECONDS
            if self._con is not None and not stale:
                return self._con
            if self._con is not None:
                self._con.close()
            self._con = self._build()
            self._built_at = time.monotonic()
            return self._con

    def _build(self) -> Any:
        import duckdb

        con = duckdb.connect(":memory:", config=self._config)
        if self._filesystem is not None and any(u.startswith("s3://") for u in self.datasets.values()):
            con.register_filesystem(self._filesystem)
        self.errors, self.columns = {}, {}
        for name, uri in self.datasets.items():
            try:
                con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{uri.replace(chr(39), '')}', "
                            "hive_partitioning = true, union_by_name = true)")
                self.columns[name] = [(r[0], r[1]) for r in con.execute(f"DESCRIBE {name}").fetchall()]
            except Exception as exc:
                con.execute(f"DROP VIEW IF EXISTS {name}")
                self.errors[name] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
                logger.warning("dataset %s unavailable: %s", name, self.errors[name])
        dirs = sorted({_directory(u) for n, u in self.datasets.items() if n not in self.errors})
        con.execute("SET allowed_directories = ?", [dirs])
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        return con

    def describe(self) -> dict[str, Any]:
        """What the model (and the console) is told: each dataset's columns, or why it is unavailable."""
        try:
            self._connection()
        except Exception as exc:  # pragma: no cover - duckdb missing or out of memory
            return {"error": type(exc).__name__}
        return {"datasets": {n: {"columns": [f"{c} {t}" for c, t in self.columns.get(n, [])]} if n in self.columns
                             else {"unavailable": self.errors.get(n, "unknown")} for n in self.names},
                "max_rows": self.max_rows}

    def summary(self) -> dict[str, Any]:
        """Like ``describe`` but never opens anything (safe on the event loop): names only until first use."""
        if self._con is None:
            return {n: {} for n in self.names}
        return {n: ({"columns": [f"{c} {t}" for c, t in self.columns[n]]} if n in self.columns
                    else {"unavailable": self.errors.get(n, "unknown")}) for n in self.names}

    # -------------------------------------------------------------------------------- query
    def _run(self, sql: str) -> dict[str, Any]:
        if not self._slots.acquire(timeout=self.timeout):
            raise QueryFailed("busy", "Too many queries are running; try again shortly.")
        try:
            base = self._connection()
            available = [n for n in self.names if n in self.columns]
            cursor = base.cursor()
            timer = threading.Timer(self.timeout, cursor.interrupt)
            try:
                text = check_select(cursor, sql, available)
                timer.start()
                res = cursor.execute(f"SELECT * FROM ({text}) AS q LIMIT {self.max_rows + 1}")
                cols = [d[0] for d in res.description]
                rows = res.fetchall()
            except SqlRejected:
                raise
            except Exception as exc:
                if "interrupt" in str(exc).lower():
                    raise QueryFailed("timeout", f"The query took longer than {self.timeout:g}s.") from exc
                raise QueryFailed("error", f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}") from exc
            finally:
                timer.cancel()
                cursor.close()
        finally:
            self._slots.release()
        cells = [[self._cell(v) for v in row] for row in rows[: self.max_rows]]
        return {"columns": cols, "rows": cells, "row_count": len(cells), "truncated": len(rows) > self.max_rows}

    def _cell(self, value: Any) -> Any:
        value = _jsonable(value)
        if isinstance(value, str):
            return redact(value, self.max_cell_chars) or value
        if isinstance(value, (list, dict)):
            return redact(str(value), self.max_cell_chars)
        return value

    async def query(self, sql: str) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(self._run, sql)
        except SqlRejected as exc:
            _ops.add(1, {"outcome": f"rejected_{exc.reason}"})
            raise
        except QueryFailed as exc:
            _ops.add(1, {"outcome": exc.reason})
            raise
        _ops.add(1, {"outcome": "ok"})
        return result

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                self._con.close()
                self._con = None
