"""MCP servers through the gateway, with a fresh Entra token on every request.

MAF's ``MCPStreamableHTTPTool`` takes static headers, which expire with the token after about an
hour. This helper gives it an ``httpx`` client whose auth flow asks ``ToolAuth`` for headers on
every request, and adds the agentkit identification headers.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Collection, Mapping
from typing import Any

import httpx
from agent_framework import MCPStreamableHTTPTool

from .auth import ToolAuth

__all__ = ["DynamicAuth", "ResilientMCPTool", "gateway_mcp_tool", "platform_connectors"]

logger = logging.getLogger(__name__)


class DynamicAuth(httpx.Auth):
    """httpx auth that fetches headers from a ToolAuth for each request."""

    def __init__(self, auth: ToolAuth) -> None:
        self._auth = auth

    async def async_auth_flow(self, request: httpx.Request):
        for key, value in (await self._auth.headers()).items():
            request.headers[key] = value
        yield request

    def sync_auth_flow(self, request):  # pragma: no cover - MCP client is async-only
        raise RuntimeError("DynamicAuth is async-only")


def gateway_mcp_tool(
    name: str,
    url: str,
    *,
    auth: ToolAuth,
    agent_name: str | None = None,
    team: str | None = None,
    allowed_tools: Collection[str] | None = None,
    approval_mode: str | None = None,
    extra_headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    **kwargs: Any,
) -> MCPStreamableHTTPTool:
    """An MCP server (ideally fronted by APIM) as a MAF tool source.

    Always pass ``allowed_tools``. MCP servers can expose many tools, and the agent should only
    see the ones its charter covers.
    """
    headers = dict(extra_headers or {})
    if agent_name:
        headers["x-agentkit-agent"] = agent_name
    if team:
        headers["x-agentkit-team"] = team
    client = httpx.AsyncClient(auth=DynamicAuth(auth), headers=headers, timeout=timeout)
    return MCPStreamableHTTPTool(
        name=name,
        url=url,
        http_client=client,
        allowed_tools=allowed_tools,
        approval_mode=approval_mode,
        **kwargs,
    )


# ------------------------------------------------------------------ connectors from the platform (deploy/vps)
class ResilientMCPTool(MCPStreamableHTTPTool):
    """An MCP server that can't take the agent down with it.

    MAF connects an MCP tool once (entering it into the agent's exit stack) and keeps the connection; a
    failed connection fails the run. A connector (a CRM, an issue tracker) being down must not stop the
    agent answering everything else, so this one:

    * on a failed connection, logs it and reports itself settled to MAF (so MAF doesn't enter it again on
      every run), offering no tools;
    * before each run, once ``retry_seconds`` have passed, reconnects by itself.
    """

    def __init__(self, *args: Any, retry_seconds: float = 60.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._retry_seconds = retry_seconds
        self._down_until = 0.0
        self.available = True

    async def _reachable(self) -> None:
        """Any HTTP answer (even 401 or 405) means the server is there; only a failed connection doesn't."""
        async with httpx.AsyncClient(timeout=3.0) as probe:
            await probe.request("GET", self.url)

    def _mark_down(self, exc: BaseException) -> None:
        self.available = False
        self._down_until = time.monotonic() + self._retry_seconds
        self.is_connected = True  # settled, as far as MAF is concerned: it mustn't enter us again each run
        logger.warning("connector %s is unavailable, running without it for %.0f s: %s",
                       self.name, self._retry_seconds, str(exc)[:200])

    async def _try_connect(self) -> None:
        try:
            await self._reachable()  # a quick check first: a failed MCP handshake is slow and noisy
            self.is_connected = False
            await self.connect()
            self.available = True
        except Exception as exc:  # noqa: BLE001 - any failure means "run without it"
            with contextlib.suppress(BaseException):
                await self.close()
            self._mark_down(exc)

    async def __aenter__(self):  # type: ignore[override]
        await self._try_connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:  # type: ignore[override]
        if self.available:
            with contextlib.suppress(Exception):
                await super().__aexit__(*exc)
        return None

    async def _prepare_for_run(self, *args: Any, **kwargs: Any) -> None:  # called by MAF before every run
        if not self.available:
            if time.monotonic() < self._down_until:
                return
            await self._try_connect()
            if not self.available:
                return
        with contextlib.suppress(Exception):
            await super()._prepare_for_run(*args, **kwargs)

    @property
    def functions(self):  # type: ignore[override]
        return super().functions if self.available else []


def platform_connectors(value: str | None, token: str | None, *, retry_seconds: float = 60.0) -> list[ResilientMCPTool]:
    """The connectors the VPS platform assigned to this agent (``AGENTKIT_CONNECTORS``, JSON).

    Each entry is ``{"name", "url", "title", "allowed_tools": [...], "approval": [...]}``: the url is the
    platform's connector gateway, which adds the vendor credential and only passes the allowed tools, so
    the agent never holds a vendor secret. ``token`` identifies this agent to the gateway. Tools that
    change something (``approval``) pause for the person to confirm, like any other approval.
    """
    if not value:
        return []
    try:
        entries = json.loads(value)
    except ValueError:
        logger.warning("AGENTKIT_CONNECTORS isn't valid JSON; no connectors loaded")
        return []
    tools: list[ResilientMCPTool] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        name, url = str(entry.get("name") or ""), str(entry.get("url") or "")
        if not name.isidentifier() or not url.startswith(("http://", "https://")):
            logger.warning("skipping connector entry %r", entry.get("name"))
            continue
        allowed = [str(t) for t in entry.get("allowed_tools") or []]
        approval = [t for t in (str(x) for x in entry.get("approval") or []) if t in allowed]
        headers = {"authorization": f"Bearer {token}"} if token else {}
        if entry.get("policy"):  # the tool rules this agent was started with: the gateway refuses stale ones
            headers["x-agentkit-connector-policy"] = str(entry["policy"])
        tools.append(ResilientMCPTool(
            name=name, url=url, tool_name_prefix=name, description=entry.get("title") or name,
            allowed_tools=allowed, load_prompts=False, retry_seconds=retry_seconds,
            approval_mode={"always_require_approval": approval,
                           "never_require_approval": [t for t in allowed if t not in approval]},
            http_client=httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30.0, read=120.0)),
        ))
    return tools
