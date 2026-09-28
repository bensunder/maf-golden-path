"""Connectors: MCP servers (a CRM, an issue tracker, a database) that agents on this server may use.

An admin adds a connector in the console: its MCP server URL, how it signs in (none, a bearer token, or an
API-key header), and which of its tools agents may use, each marked read or write. The credential is
encrypted here (``PLATFORM_SECRET_KEY``) and never leaves this service: agents call the **connector
gateway** (port 8001, only on the Docker network) with their own token, and the gateway

* checks the agent is assigned that connector, and was started with its current tool rules,
* passes only the MCP messages an agent needs (initialize, ping, tools/list, tools/call, notifications),
  re-encoded from what it checked, to the connector's URL exactly as stored (no extra path or query),
* refuses a ``tools/call`` for a tool that isn't allowed, and leaves such tools out of ``tools/list``,
* adds the vendor credential and forwards the request to the vendor,
* records the call (agent, connector, tool, allowed or refused).

Write tools pause for the person in the chat to approve (the agent's side, ``agentkit.tools.platform_connectors``).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

CONNECTOR_NAME = re.compile(r"[a-z][a-z0-9_]{1,30}")  # also the prefix of its tools in the agent: an identifier
TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
AUTH_KINDS = ("none", "bearer", "header")
ALLOW_PRIVATE = os.getenv("PLATFORM_CONNECTOR_ALLOW_PRIVATE") == "1"  # development only: http and private addresses
MAX_BODY = 1_000_000
MAX_LISTING = 4_000_000  # a tools/list answer, read whole to take out the tools that aren't allowed
_METHODS = {"initialize", "ping", "tools/list", "tools/call", "notifications/initialized", "notifications/cancelled",
            "notifications/progress", "notifications/roots/list_changed"}
_MESSAGE_KEYS = {"jsonrpc", "id", "method", "params", "result", "error"}
_PARAM_KEYS = {"name", "arguments", "_meta", "cursor", "protocolVersion", "capabilities", "clientInfo", "requestId",
               "reason", "progressToken", "progress", "total", "message"}
_PASS = {"content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id"}  # to the vendor
_BACK = {"content-type", "mcp-session-id", "cache-control"}  # to the agent


class ConnectorError(ValueError):
    pass


# ------------------------------------------------------------------------------------------ secrets
def policy_of(entry: dict) -> str:
    """A fingerprint of a connector's tool rules. Agents send the one they were started with; the gateway
    refuses a stale one, so a tool that newly needs approval can't be called by an agent that doesn't know."""
    rules = json.dumps({"allowed": sorted(entry.get("allowed") or []), "approval": sorted(entry.get("approval") or [])},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rules.encode()).hexdigest()[:16]


class Vault:
    """Encrypts connector credentials with PLATFORM_SECRET_KEY (Fernet: AES-128-CBC + HMAC-SHA256)."""

    def __init__(self, key: str | None):
        from cryptography.fernet import Fernet

        self.error: str | None = None
        try:
            self._fernet = Fernet(key.encode()) if key else None
        except (ValueError, TypeError):  # a broken key must not stop the router: only connectors stop
            self._fernet = None
            self.error = "PLATFORM_SECRET_KEY isn't a valid key: run agentctl.py platform to create one"

    @property
    def ready(self) -> bool:
        return self._fernet is not None

    def seal(self, secret: str) -> str:
        if not self._fernet:
            raise ConnectorError("PLATFORM_SECRET_KEY isn't set: run agentctl.py platform again")
        return self._fernet.encrypt(secret.encode()).decode()

    def open(self, sealed: str) -> str:
        from cryptography.fernet import InvalidToken

        if not self._fernet:
            raise ConnectorError(self.error or "PLATFORM_SECRET_KEY isn't set")
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except InvalidToken:
            raise ConnectorError("the stored credential can't be decrypted (was PLATFORM_SECRET_KEY changed?): "
                                 "replace the credential on the connector's page") from None


# ------------------------------------------------------------------------------------------ safe URLs
_RESOLVED: dict[str, tuple[float, list[str]]] = {}


async def check_url(url: str) -> None:
    """https to a public address only: a connector must not become a way into this server's own network
    (Redis, the Docker network, cloud metadata). Checked when it's saved and on every call."""
    parts = urlsplit(url)
    if parts.scheme not in ("https",) + (("http",) if ALLOW_PRIVATE else ()):
        raise ConnectorError("the MCP server URL must start with https://")
    if not parts.hostname or parts.username or parts.password:
        raise ConnectorError("the MCP server URL needs a host name, without a user name or password in it")
    if ALLOW_PRIVATE:
        return
    host = parts.hostname
    cached = _RESOLVED.get(host)
    if cached and time.monotonic() - cached[0] < 60:
        addresses = cached[1]
    else:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or 443)
        except OSError:
            raise ConnectorError(f"{host} can't be resolved") from None
        addresses = sorted({i[4][0] for i in infos})
        _RESOLVED[host] = (time.monotonic(), addresses)
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            raise ConnectorError(f"{host} points to a private or reserved address; connectors must be public services")


# ------------------------------------------------------------------------------------------ store
class Store:
    """connectors.json next to agents.yaml (owner-only). Secrets are sealed; nothing here is secret in the clear."""

    def __init__(self, path: Path, vault: Vault):
        self.path, self.vault = path, vault

    def load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        return dict(data.get("connectors") or {})

    def save(self, connectors: dict[str, dict]) -> None:
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps({"connectors": connectors}, indent=2, sort_keys=True), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def get(self, name: str) -> dict | None:
        return self.load().get(name)

    def credential_headers(self, entry: dict) -> dict[str, str]:
        if entry.get("auth") == "bearer" and entry.get("secret"):
            return {"authorization": f"Bearer {self.vault.open(entry['secret'])}"}
        if entry.get("auth") == "header" and entry.get("secret") and entry.get("header"):
            return {entry["header"]: self.vault.open(entry["secret"])}
        return {}


def public_view(name: str, entry: dict, used_by: list[str], *, admin: bool = False) -> dict:
    """What anyone signed in may see: never the credential, only whether one is set. The full URL only for
    admins: some hosted MCP servers carry a key in it."""
    return {"name": name, "title": entry.get("title") or name, "url": entry.get("url") if admin else None,
            "host": urlsplit(entry.get("url") or "").hostname, "auth": entry.get("auth"), "header": entry.get("header"),
            "has_secret": bool(entry.get("secret")), "tools": entry.get("tools") or [], "allowed": entry.get("allowed") or [],
            "approval": entry.get("approval") or [], "preset": entry.get("preset"), "created_by": entry.get("created_by"),
            "created_at": entry.get("created_at"), "used_by": used_by}


def validate_entry(name: str, body: dict, *, existing: dict | None = None) -> dict:
    """The connector as it will be stored, from what the console sent. Raises ConnectorError."""
    if not CONNECTOR_NAME.fullmatch(name):
        raise ConnectorError("name: 2–31 lowercase letters, digits or underscores, starting with a letter")
    entry = dict(existing or {})
    for key in ("title", "url", "auth", "header", "preset"):
        if key in body and body[key] is not None:
            entry[key] = str(body[key]).strip()
    title = entry.get("title") or name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .,()&+/-]{0,59}", title):
        raise ConnectorError("title: up to 60 letters, digits, spaces and . , ( ) & + / -")
    entry["title"] = title
    if len(entry.get("url") or "") > 500:
        raise ConnectorError("url: too long")
    if entry.get("auth") not in AUTH_KINDS:
        raise ConnectorError("auth: none, bearer or header")
    if entry["auth"] == "header":
        if not HEADER_NAME.fullmatch(entry.get("header") or "") or entry["header"].lower() in (
                "host", "cookie", "content-type", "accept", "mcp-session-id", "mcp-protocol-version", "content-length"):
            raise ConnectorError("header: the API-key header's name, e.g. X-API-Key")
    else:
        entry.pop("header", None)
    if entry.get("preset") and not re.fullmatch(r"[a-z0-9_-]{1,40}", entry["preset"]):
        raise ConnectorError("preset: unexpected value")
    tools = body.get("tools", entry.get("tools") or [])
    clean_tools = []
    for t in tools if isinstance(tools, list) else []:
        if isinstance(t, dict) and TOOL_NAME.fullmatch(str(t.get("name") or "")):
            clean_tools.append({"name": str(t["name"]), "description": str(t.get("description") or "")[:500],
                                "read_only": bool(t.get("read_only"))})
    entry["tools"] = clean_tools[:500]
    known = {t["name"] for t in clean_tools}
    for key in ("allowed", "approval"):
        values = body.get(key, entry.get(key) or [])
        if not isinstance(values, list) or not all(isinstance(v, str) and TOOL_NAME.fullmatch(v) for v in values):
            raise ConnectorError(f"{key}: a list of tool names")
        unknown = [v for v in values if known and v not in known]
        if unknown:
            raise ConnectorError(f"{key}: not tools of this server: {', '.join(unknown[:5])}")
        entry[key] = sorted(set(values))
    entry["approval"] = [t for t in entry["approval"] if t in entry["allowed"]]
    return entry


# ------------------------------------------------------------------------------------------ discovery
async def list_tools(url: str, headers: dict[str, str], timeout: float = 20.0) -> list[dict]:
    """Connect to an MCP server as a client and list its tools (name, description, read-only hint)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    await check_url(url)

    async def run() -> list[dict]:
        async with streamablehttp_client(url, headers=headers, timeout=timeout) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                found, cursor = [], None
                while True:
                    page = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
                    for tool in page.tools:
                        hints = tool.annotations
                        found.append({"name": tool.name, "description": (tool.description or "")[:500],
                                      "read_only": bool(hints and hints.readOnlyHint)})
                    cursor = getattr(page, "nextCursor", None)
                    if not cursor or len(found) > 500:
                        return found

    try:
        return await asyncio.wait_for(run(), timeout + 5)
    except ConnectorError:
        raise
    except BaseException as exc:  # noqa: BLE001 - surface one readable reason
        detail = exc
        while isinstance(detail, BaseExceptionGroup) and detail.exceptions:
            detail = detail.exceptions[0]
        text = str(detail) or type(detail).__name__
        if "401" in text or "403" in text:
            text = "the server refused the credential (401/403)"
        raise ConnectorError(f"couldn't list the server's tools: {text[:200]}") from None


# ------------------------------------------------------------------------------------------ gateway
class Activity:
    def __init__(self, size: int = 500):
        self.items: deque[dict] = deque(maxlen=size)

    def add(self, **item: Any) -> None:
        item["tool"] = str(item.get("tool") or "")[:128]
        self.items.append({"at": time.time(), **item})

    def for_connector(self, name: str) -> list[dict]:
        return [i for i in reversed(self.items) if i["connector"] == name][:100]


def _calls(payload: Any) -> list[tuple[Any, str]]:
    """(id, tool name) for every tools/call in a JSON-RPC message or batch."""
    messages = payload if isinstance(payload, list) else [payload]
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("method") == "tools/call":
            params = m.get("params") if isinstance(m.get("params"), dict) else {}
            out.append((m.get("id"), str(params.get("name") or "")[:128]))
    return out


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def _suspicious(obj: dict, canonical: set[str]) -> bool:
    """A key that differs only in case from one the vendor may read ("Name" beside "name"): parsers disagree."""
    lowered = {c.lower(): c for c in canonical}
    return any(k.lower() in lowered and k != lowered[k.lower()] for k in obj)


def parse_messages(body: bytes) -> tuple[Any, list[dict]]:
    """The JSON-RPC message(s), checked strictly: what's forwarded is re-encoded from exactly this."""
    try:
        payload = json.loads(body or b"null", object_pairs_hook=_no_duplicates)
    except ValueError as exc:
        raise ConnectorError(f"not a valid JSON-RPC message ({exc})") from None
    messages = payload if isinstance(payload, list) else [payload]
    if not messages or not all(isinstance(m, dict) for m in messages):
        raise ConnectorError("not a JSON-RPC message")
    for m in messages:
        params = m.get("params")
        if _suspicious(m, _MESSAGE_KEYS) or (isinstance(params, dict) and _suspicious(params, _PARAM_KEYS)):
            raise ConnectorError("ambiguous JSON-RPC message")
        if "method" in m:
            if m["method"] not in _METHODS:
                raise ConnectorError(f"method {str(m['method'])[:60]!r} isn't available through the gateway")
        elif not ("id" in m and ("result" in m or "error" in m)):
            raise ConnectorError("not a JSON-RPC request, notification or response")
    return payload, messages


def _filter_listing(message: Any, allowed: set[str]) -> Any:
    if isinstance(message, dict) and isinstance(message.get("result"), dict) and isinstance(message["result"].get("tools"), list):
        message["result"]["tools"] = [t for t in message["result"]["tools"] if isinstance(t, dict) and t.get("name") in allowed]
    return message


def filter_tools_list(body: bytes, content_type: str, allowed: set[str]) -> bytes:
    """Take the tools that aren't allowed out of a tools/list answer (JSON or an SSE stream of JSON)."""
    if "text/event-stream" in content_type:
        lines = []
        for line in body.decode("utf-8", "replace").split("\n"):
            if line.startswith("data:"):
                try:
                    line = "data: " + json.dumps(_filter_listing(json.loads(line[5:].strip()), allowed))
                except ValueError:
                    pass
            lines.append(line)
        return "\n".join(lines).encode()
    try:
        data = json.loads(body)
    except ValueError:
        return body
    data = [_filter_listing(m, allowed) for m in data] if isinstance(data, list) else _filter_listing(data, allowed)
    return json.dumps(data).encode()


def create_gateway(store: Store, assignments: Callable[[], dict[str, dict]], activity: Activity,
                   client: httpx.AsyncClient | None = None) -> FastAPI:
    """The connector gateway agents call. ``assignments`` maps each agent to {"token", "connectors"}."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=None), follow_redirects=False,
                                       limits=httpx.Limits(max_connections=None, max_keepalive_connections=20))

    def agent_for(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if len(token) >= 32:
            for agent, a in assignments().items():
                if a.get("token") and hmac.compare_digest(str(a["token"]), token):
                    return agent
        raise HTTPException(401, "unknown agent")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    def refuse(payload: Any, calls: list[tuple[Any, str]], text: str) -> JSONResponse:
        errors = [{"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": text.format(tool=t)}} for i, t in calls]
        if not errors:
            errors = [{"jsonrpc": "2.0", "id": m.get("id"), "error": {"code": -32600, "message": text.format(tool="")}}
                      for m in (payload if isinstance(payload, list) else [payload]) if isinstance(m, dict) and "id" in m]
        return JSONResponse(errors if isinstance(payload, list) else (errors[0] if errors else {}), status_code=200)

    async def read_body(request: Request) -> bytes:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY:
            raise HTTPException(413, "request too large")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                raise HTTPException(413, "request too large")
            chunks.append(chunk)
        return b"".join(chunks)

    # one route: the connector's URL exactly as stored. No extra path or query ever reaches the vendor.
    @app.api_route("/mcp/{name}", methods=["GET", "POST", "DELETE"])
    async def gateway(name: str, request: Request) -> Response:
        agent = agent_for(request)
        if name not in (assignments().get(agent, {}).get("connectors") or []):
            raise HTTPException(403, f"{agent} isn't assigned the {name} connector")
        entry = store.get(name)
        if not entry:
            raise HTTPException(404, f"no connector named {name}")
        body, listing = b"", False
        allowed = set(entry.get("allowed") or [])
        if request.method == "POST":
            raw = await read_body(request)
            try:
                payload, messages = parse_messages(raw)
            except ConnectorError as exc:
                raise HTTPException(400, str(exc)) from None
            calls = _calls(payload)
            wants_tools = bool(calls) or any(m.get("method") == "tools/list" for m in messages)
            if wants_tools and request.headers.get("x-agentkit-connector-policy") != policy_of(entry):
                return refuse(payload, calls, "the connector's tool rules changed: the agent is restarting with the new ones")
            refused = [(i, t) for i, t in calls if t not in allowed]
            for _, tool in calls:
                activity.add(agent=agent, connector=name, tool=tool, allowed=tool in allowed)
            if refused:
                return refuse(payload, refused, "tool {tool!r} isn't allowed for this connector")
            listing = any(m.get("method") == "tools/list" for m in messages)
            body = json.dumps(payload, separators=(",", ":")).encode()  # exactly what was checked
        try:
            await check_url(entry["url"])
            headers = {k: v for k, v in request.headers.items() if k.lower() in _PASS}
            headers.update(store.credential_headers(entry))
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from None
        if listing:
            headers["accept-encoding"] = "identity"
        try:
            upstream = await http.send(http.build_request(request.method, entry["url"], headers=headers, content=body or None),
                                       stream=True)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"the {name} server isn't answering ({type(exc).__name__})") from None

        passed = [(k, v) for k, v in upstream.headers.multi_items() if k.lower() in _BACK]
        if listing:  # read it whole, take out the tools that aren't allowed
            try:
                data = b""
                if upstream.is_stream_consumed:  # already read (some transports): decoded already
                    data = upstream.content
                else:
                    async for chunk in upstream.aiter_bytes():
                        data += chunk
                        if len(data) > MAX_LISTING:
                            raise HTTPException(502, "the tool listing is too large")
            finally:
                await upstream.aclose()
            filtered = filter_tools_list(data, upstream.headers.get("content-type", ""), allowed)
            out = Response(filtered, status_code=upstream.status_code)
            out.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in passed] + [
                (b"content-length", str(len(filtered)).encode())]
            return out

        decoded = upstream.is_stream_consumed  # already read (some transports): the body is decoded

        async def stream():
            try:
                if decoded:
                    yield upstream.content
                    return
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        kept = _BACK | ({"content-encoding", "content-length"} if not decoded else set())
        out = StreamingResponse(stream(), status_code=upstream.status_code)
        out.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in upstream.headers.multi_items()
                           if k.lower() in kept]
        return out

    return app
