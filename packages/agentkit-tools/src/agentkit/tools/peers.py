"""Calling other agents on the VPS platform, as tools.

An admin decides which agents may call which (the console's Agents page, or ``agentctl.py peers``). Each
agent this one may call becomes a tool, ``ask_<agent>``, and every call goes through the platform's gateway,
which:

* checks this agent's token and the platform-signed delegation token of the current request, so the call
  acts for the person who is actually talking to this agent, and for no one else;
* checks the admin's allow list, refuses loops (A → B → A), limits depth and the number of calls one request
  can fan out to;
* sends the request to the other agent signed, as that person. The other agent runs its own guardrails,
  tool policy, approvals and audit trail, and records which agents the request came through.

What comes back is another agent's words: this agent's tool-output shield scans them like any tool result.

When the other agent needs a person to approve an action, the gateway answers with a ticket. The model then
calls ``confirm_agent_action``, which always asks the person first (it's an ``always_require`` tool): they see
the exact actions in their own chat. The platform goes ahead only if what they approved matches what the other
agent asked for.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from agent_framework import FunctionTool
from opentelemetry import propagate

from agentkit.telemetry import get_run_context

__all__ = ["CONFIRM_TOOL", "platform_peers"]

CONFIRM_TOOL = "confirm_agent_action"
_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}[a-z0-9]")


def _peers(value: str | None) -> list[dict[str, str]]:
    try:
        entries = json.loads(value or "[]")
    except ValueError:
        return []
    out = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not _NAME.fullmatch(str(e.get("name", ""))):
            continue
        url = str(e.get("url", ""))
        if not url.startswith(("http://", "https://")):
            continue
        out.append({"name": e["name"], "title": str(e.get("title") or e["name"])[:80],
                    "description": str(e.get("description") or "")[:240], "url": url.rstrip("/")})
    return out


def _describe(actions: list[dict[str, Any]]) -> str:
    return "; ".join(f"{a.get('tool')} with {json.dumps(a.get('arguments') or {}, sort_keys=True)}" for a in actions)


def _answer(peer: dict[str, str], body: dict[str, Any]) -> str:
    title = peer["title"]
    reply = str(body.get("reply") or "").strip()
    if body.get("status") == "approval_required":
        actions = body.get("actions") or []
        text = (f"{title} needs the person's approval before it runs: {_describe(actions)}. "
                f"Agent: {peer['name']}. Approval ticket: {body.get('ticket')}. "
                f"Actions: {json.dumps(actions, sort_keys=True)}.")
        return text + (f" {title} also said: {reply}" if reply else "")
    if body.get("blocked"):
        return f"{title} declined the request ({body['blocked']})." + (f" It said: {reply}" if reply else "")
    return f"{title} answered: {reply}" if reply else f"{title} finished without a reply."


async def _post(peer: dict[str, str], path: str, payload: dict[str, Any], token: str | None, timeout: float) -> str:
    ctx = get_run_context()
    if ctx is None or not ctx.delegation:
        return (f"I can't reach {peer['title']} from here: calls between agents only work for requests that "
                "come through the platform.")
    headers = {"authorization": f"Bearer {token or ''}", "x-agentkit-delegation": ctx.delegation}
    propagate.inject(headers)  # one trace across every agent in the chain
    try:
        async with _client(timeout) as http:
            response = await http.post(f"{peer['url']}{path}", json=payload, headers=headers)
    except httpx.HTTPError:
        return f"{peer['title']} isn't answering right now. Try again later."
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200:
        detail = body.get("detail") if isinstance(body, dict) else None
        return f"{peer['title']} couldn't take the request: {detail or f'HTTP {response.status_code}'}."
    return _answer(peer, body if isinstance(body, dict) else {})


def platform_peers(value: str | None, token: str | None, *, timeout: float = 150.0) -> list[FunctionTool]:
    """Tools for the agents this one may call (``AGENTKIT_PEERS``), plus the approval tool when there are any."""
    peers = _peers(value)
    if not peers:
        return []
    by_name = {p["name"]: p for p in peers}
    tools: list[FunctionTool] = []
    for peer in peers:
        async def ask(request: str, _peer: dict[str, str] = peer) -> str:
            return await _post(_peer, "/turn", {"message": request[:20_000], "conversation": _conversation()},
                               token, timeout)

        tools.append(FunctionTool(
            name=f"ask_{peer['name'].replace('-', '_')}",
            description=(f"Ask {peer['title']}, another agent on this platform"
                         + (f" ({peer['description']})" if peer["description"] else "")
                         + ". It acts for the same person, with its own tools, rules and approvals. It doesn't see "
                           "this conversation: send one clear, self-contained request."),
            func=ask,
            additional_properties={"agentkit.peer": peer["name"]},
            input_model={"type": "object", "properties": {"request": {"type": "string", "description":
                         "What you need from it, in full"}}, "required": ["request"]},
        ))

    async def confirm(agent: str, ticket: str, actions: list[dict[str, Any]]) -> str:
        peer = by_name.get(agent)
        if peer is None:
            return f"There's no agent called {agent} that this agent may call."
        return await _post(peer, "/decide", {"ticket": ticket, "approved": True, "actions": actions}, token, timeout)

    tools.append(FunctionTool(
        name=CONFIRM_TOOL,
        description=("When another agent's answer says it needs the person's approval, tell the person what it wants "
                     "to do and call this with that agent's name, the approval ticket and the actions exactly as "
                     "listed. The person is always asked to approve before anything runs, and the platform only goes "
                     "ahead if what they approved matches what the other agent asked for."),
        func=confirm,
        approval_mode="always_require",
        additional_properties={"agentkit.peer": "*"},
        input_model={"type": "object", "properties": {
            "agent": {"type": "string"}, "ticket": {"type": "string"},
            "actions": {"type": "array", "items": {"type": "object", "properties": {
                "tool": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["tool", "arguments"]}}},
            "required": ["agent", "ticket", "actions"]},
    ))
    return tools


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def _conversation() -> str | None:
    ctx = get_run_context()
    return ctx.session_id if ctx else None
