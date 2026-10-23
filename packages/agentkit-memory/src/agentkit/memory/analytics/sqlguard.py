"""Read-only SQL, checked before it runs.

Agents (``query_analytics``) and memory admins (``POST /v1/analytics/query``) may send SQL. Before anything
executes, DuckDB's own parser turns the text into a syntax tree (``json_serialize_sql``) and the tree is walked:

* exactly one statement, and it must be a SELECT (DuckDB refuses to serialize anything else, so INSERT, COPY,
  ATTACH, INSTALL, PRAGMA, SET and the rest never get this far);
* every table it reads is one of the allowed names (or a CTE it defines): no ``read_csv('/etc/passwd')``,
  no ``'s3://other-bucket/x.parquet'`` replacement scans, no other catalog or schema;
* no table functions at all, and no scalar functions that reach outside the data (``getenv``,
  ``current_setting``, file readers...).

The connection it then runs on is locked down too (no file access outside the datasets' directories,
configuration locked), so this check is the first of two walls, not the only one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

__all__ = ["SqlRejected", "check_select"]

#: Scalar functions that read the environment, the file system or settings: never allowed in agent or admin SQL.
DENIED_FUNCTIONS = frozenset({
    "getenv", "current_setting", "duckdb_settings", "read_text", "read_blob", "glob", "read_csv", "read_csv_auto",
    "read_parquet", "parquet_scan", "read_json", "read_json_auto", "read_json_objects", "sniff_csv", "query",
    "query_table", "duckdb_extensions", "duckdb_secrets", "which_secret", "pragma_version", "load_extension",
    "duckdb_databases", "duckdb_temporary_files", "parquet_metadata", "parquet_schema", "parquet_file_metadata",
    "parquet_kv_metadata", "read_xlsx", "sqlite_scan", "postgres_scan", "mysql_scan", "iceberg_scan", "delta_scan",
})


class SqlRejected(ValueError):
    """The SQL isn't a single read-only SELECT over the allowed tables. ``reason`` is a stable code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _walk(node: Any) -> Iterable[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def check_select(connection: Any, sql: str, allowed_tables: Iterable[str], *, max_chars: int = 8000) -> str:
    """The SQL, stripped, if it is safe to run; ``SqlRejected`` otherwise. ``connection`` is any DuckDB
    connection (only its parser is used)."""
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise SqlRejected("empty", "Send one SELECT statement.")
    if len(text) > max_chars:
        raise SqlRejected("too_long", f"Keep the query under {max_chars} characters.")
    raw = connection.execute("SELECT json_serialize_sql(?)", [text]).fetchone()[0]
    tree = json.loads(raw)
    if tree.get("error"):
        message = str(tree.get("error_message") or "")
        if "Only SELECT" in message:
            raise SqlRejected("not_select", "Only SELECT queries are allowed.")
        raise SqlRejected("syntax", f"The query doesn't parse: {message[:200]}")
    statements = tree.get("statements") or []
    if len(statements) != 1:
        raise SqlRejected("multiple", "Send exactly one statement.")
    allowed = {t.lower() for t in allowed_tables}
    ctes = {str(entry.get("key", "")).lower() for node in _walk(statements[0])
            if isinstance(node.get("cte_map"), dict) for entry in node["cte_map"].get("map") or []}
    for node in _walk(statements[0]):
        kind = node.get("type")
        if kind == "TABLE_FUNCTION":
            raise SqlRejected("table_function", "Table functions aren't allowed; query the listed tables.")
        if kind == "BASE_TABLE":
            name = str(node.get("table_name") or "").lower()
            schema = str(node.get("schema_name") or "").lower()
            catalog = str(node.get("catalog_name") or "").lower()
            if catalog or schema not in ("", "main") or (name not in allowed and name not in ctes):
                shown = ".".join(p for p in (catalog, schema, name) if p)
                raise SqlRejected("table", f"{shown!r} isn't available. Tables: {', '.join(sorted(allowed))}.")
        if node.get("class") == "FUNCTION":
            name = str(node.get("function_name") or "").lower()
            schema = str(node.get("schema") or "").lower()
            if name in DENIED_FUNCTIONS or schema not in ("", "main"):
                raise SqlRejected("function", f"The function {name!r} isn't allowed here.")
    return text
