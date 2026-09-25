"""Turn an OpenAPI 3 description into MAF tools.

    orders = ApiClient("https://api.contoso.com/orders", auth=ManagedIdentityAuth("api://orders/.default"))
    TOOLS += openapi_tools("specs/orders.yaml", client=orders,
                           operations=["getOrder", "listOrders", "cancelOrder"],
                           allow_writes=["cancelOrder"],
                           shapers={"listOrders": Shaper(fields=["id", "status"], items_key="value")})

Read-only by default: POST/PUT/PATCH/DELETE operations are skipped unless listed in
``allow_writes``, and asking for a write in ``operations`` without allowing it raises an error.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from agent_framework import FunctionTool, tool

from .http import ApiClient, ToolHttpError
from .shaping import DEFAULT_SHAPER, Shaper

__all__ = ["OpenApiOperation", "load_spec", "openapi_operations", "openapi_tools"]

WRITE_METHODS = {"post", "put", "patch", "delete"}
_METHODS = ["get", "post", "put", "patch", "delete", "head"]
_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def load_spec(spec: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(spec, Mapping):
        return dict(spec)
    text = Path(spec).read_text(encoding="utf-8")
    return json.loads(text) if str(spec).endswith(".json") else yaml.safe_load(text)


def _resolve(node: Any, spec: dict, depth: int = 0) -> Any:
    """Inline local $refs (#/components/...). Cycles are cut at depth 8."""
    if depth > 8:
        return {"type": "object"}
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str) and node["$ref"].startswith("#/"):
            target: Any = spec
            for part in node["$ref"][2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return _resolve(target, spec, depth + 1)
        return {k: _resolve(v, spec, depth) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, spec, depth) for v in node]
    return node


_SCHEMA_KEYS = {"type", "format", "enum", "items", "properties", "required", "minimum", "maximum",
                "minLength", "maxLength", "pattern", "default", "description", "additionalProperties",
                "oneOf", "anyOf", "allOf", "nullable"}


def _clean_schema(schema: Any) -> Any:
    """Keep the JSON-schema subset models understand; drop OpenAPI-only keys (example, xml, readOnly…)."""
    if isinstance(schema, dict):
        out = {k: _clean_schema(v) for k, v in schema.items() if k in _SCHEMA_KEYS}
        if "properties" in schema:
            out["properties"] = {k: _clean_schema(v) for k, v in schema["properties"].items()
                                 if not (isinstance(v, dict) and v.get("readOnly"))}
        return out
    if isinstance(schema, list):
        return [_clean_schema(s) for s in schema]
    return schema


class OpenApiOperation:
    def __init__(self, path: str, method: str, op: dict[str, Any], spec: dict[str, Any]) -> None:
        self.path = path
        self.method = method
        self.operation_id: str = op.get("operationId") or f"{method}_{path}"
        self.summary = (op.get("summary") or "").strip()
        self.description = (op.get("description") or "").strip()
        self.is_write = method in WRITE_METHODS
        params = [_resolve(p, spec) for p in op.get("parameters", [])]
        self.params = [p for p in params if p.get("in") in ("path", "query")]
        body = _resolve(op.get("requestBody"), spec) if op.get("requestBody") else None
        self.body_schema = None
        self.body_required = False
        if body:
            content = body.get("content", {})
            media = content.get("application/json") or next(iter(content.values()), None)
            if media and "schema" in media:
                self.body_schema = _clean_schema(media["schema"])
                self.body_required = bool(body.get("required"))

    @property
    def tool_name(self) -> str:
        snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", self.operation_id).lower()
        return _NAME_RE.sub("_", snake).strip("_")[:64]

    def json_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in self.params:
            schema = _clean_schema(p.get("schema") or {"type": "string"})
            if p.get("description"):
                schema = {**schema, "description": p["description"]}
            properties[p["name"]] = schema
            if p.get("required") or p.get("in") == "path":
                required.append(p["name"])
        if self.body_schema is not None:
            properties["body"] = self.body_schema
            if self.body_required:
                required.append("body")
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def tool_description(self) -> str:
        text = self.summary or self.operation_id
        if self.description and self.description != self.summary:
            text = f"{text}. {self.description}"
        if self.is_write:
            text += " (This changes data.)"
        return text[:1024]


def openapi_operations(spec: str | Path | Mapping[str, Any]) -> list[OpenApiOperation]:
    doc = load_spec(spec)
    ops = []
    for path, item in (doc.get("paths") or {}).items():
        shared = item.get("parameters", [])
        for method in _METHODS:
            if method in item:
                op = dict(item[method])
                op["parameters"] = [*shared, *op.get("parameters", [])]
                ops.append(OpenApiOperation(path, method, op, doc))
    return ops


def openapi_tools(
    spec: str | Path | Mapping[str, Any],
    *,
    client: ApiClient,
    operations: Iterable[str] | None = None,
    allow_writes: Iterable[str] = (),
    shapers: Mapping[str, Shaper] | None = None,
    default_shaper: Shaper = DEFAULT_SHAPER,
    name_prefix: str = "",
) -> list[FunctionTool]:
    """One MAF tool per selected operation. Calls go through ``client`` (auth, retries, tracing)."""
    all_ops = openapi_operations(spec)
    by_id = {op.operation_id: op for op in all_ops}
    allow_writes = set(allow_writes)
    shapers = dict(shapers or {})

    if operations is not None:
        wanted = list(operations)
        unknown = [o for o in wanted if o not in by_id]
        if unknown:
            raise ValueError(f"operations not in spec: {unknown}; available: {sorted(by_id)}")
        blocked = [o for o in wanted if by_id[o].is_write and o not in allow_writes]
        if blocked:
            raise ValueError(f"{blocked} change data; add them to allow_writes to expose them to the agent")
        selected = [by_id[o] for o in wanted]
    else:
        selected = [op for op in all_ops if not op.is_write or op.operation_id in allow_writes]

    for name in set(allow_writes) | set(shapers):
        if name not in by_id:
            raise ValueError(f"'{name}' is not an operationId in the spec")

    tools = []
    for op in selected:
        tools.append(_make_tool(op, client, shapers.get(op.operation_id, default_shaper), name_prefix))
    names = [t.name for t in tools]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate tool names after normalisation: {names}")
    return tools


def _make_tool(op: OpenApiOperation, client: ApiClient, shaper: Shaper, prefix: str) -> FunctionTool:
    path_names = [p["name"] for p in op.params if p["in"] == "path"]
    query_names = [p["name"] for p in op.params if p["in"] == "query"]

    async def call(**kwargs: Any) -> str:
        path = op.path
        for name in path_names:
            path = path.replace("{" + name + "}", quote(str(kwargs[name]), safe=""))
        params = {name: kwargs.get(name) for name in query_names}
        try:
            data = await client.request(op.method, path, params=params, json=kwargs.get("body"))
        except ToolHttpError as exc:
            return f"Error calling {op.operation_id}: {exc}"
        return shaper.shape(data)

    return tool(
        call,
        name=f"{prefix}{op.tool_name}",
        description=op.tool_description(),
        schema=op.json_schema(),
        additional_properties={"agentkit.openapi.operation": op.operation_id, "agentkit.writes": op.is_write},
    )
