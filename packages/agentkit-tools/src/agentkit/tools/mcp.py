"""MCP servers through the gateway, with a fresh Entra token on every request.

MAF's ``MCPStreamableHTTPTool`` takes static headers, which expire with the token after about an
hour. This helper gives it an ``httpx`` client whose auth flow asks ``ToolAuth`` for headers on
every request, and adds the agentkit identification headers.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

import httpx
from agent_framework import MCPStreamableHTTPTool

from .auth import ToolAuth

__all__ = ["DynamicAuth", "gateway_mcp_tool"]


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
