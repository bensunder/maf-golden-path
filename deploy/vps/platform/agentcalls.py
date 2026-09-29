"""Agents calling agents, through the platform's gateway (port 8001, on the Docker network only).

An agent may call another only if an admin allowed it (``peers`` in agents.yaml). Every call is checked here:

* **Who is calling:** the calling agent's own token (the one it uses for connectors).
* **For whom:** the platform-signed delegation token of the request the calling agent is handling. The
  platform issued it to that agent for that request (the router mints one per request), so an agent can only
  act for the person actually talking to it, and only while that request lasts.
* **Allowed, and no runaway chains:** the admin's allow list; no loops (A → B → A); at most ``MAX_DEPTH``
  agents deep; at most ``MAX_CALLS`` agent calls fanned out from one person's request.
* **Delivered as that person, signed:** the other agent receives the request as the same user, with a new
  delegation token recording the chain, signed with its own key. It runs its own guardrails, tool policy and
  approvals, and its audit trail records which agents the request came through.
* **Approvals go back to the person:** when the other agent pauses for an approval, the caller gets a ticket
  and the exact actions. The caller's ``confirm_agent_action`` tool always asks the person in their own chat;
  the decision is carried out only if the actions they approved match the ticket exactly.

Every call is recorded (who, which agents, outcome), for the console's Agent network page.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from security import DELEGATION_HEADER, delegation_key, mint, signed_headers, verify

__all__ = ["MAX_CALLS", "MAX_DEPTH", "CallLog", "install_agent_calls"]

MAX_DEPTH = 3  # agents in a chain after the first one: A → B → C → D at most
MAX_CALLS = 8  # agent calls one person's request can fan out to, in total
TICKET_SECONDS = 3600
SESSION_SECONDS = 3600
CALL_TIMEOUT = 150.0
FLEET_CALLER = "fleet@agentkit.local"
FLEET_PATHS = {"readyz", "v1/console/overview", "v1/console/evals"}
_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}[a-z0-9]")
_CONVERSATION = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class CallLog:
    """Recent agent-to-agent calls (in memory): who, through which agents, and what happened."""

    def __init__(self, size: int = 1000) -> None:
        self.items: deque[dict[str, Any]] = deque(maxlen=size)

    def add(self, **item: Any) -> None:
        self.items.append({"at": time.time(), **item})

    def recent(self, user: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        return [i for i in reversed(self.items) if user is None or i.get("user") == user][:limit]


def _strict_json(raw: bytes) -> dict[str, Any]:
    def no_duplicates(pairs):
        seen = {}
        for k, v in pairs:
            if k in seen:
                raise ValueError(f"duplicate key {k!r}")
            seen[k] = v
        return seen

    try:
        value = json.loads(raw or b"{}", object_pairs_hook=no_duplicates)
    except ValueError as exc:
        raise HTTPException(400, f"invalid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "send a JSON object")
    return value


def _canonical(actions: Any) -> str:
    return json.dumps(actions, sort_keys=True, separators=(",", ":"))


def install_agent_calls(
    app: FastAPI,
    *,
    secret: str,
    registry: Callable[[], dict],
    assignments: Callable[[], dict[str, dict]],
    upstream: Callable[[str], str],
    is_fleet: Callable[[str], Awaitable[bool]],
    log: CallLog,
    client: httpx.AsyncClient | None = None,
) -> None:
    key = delegation_key(secret)
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=CALL_TIMEOUT), follow_redirects=False)
    calls_per_root: dict[str, tuple[float, int]] = {}
    sessions: dict[tuple[str, str, str, str], tuple[str, float]] = {}
    tickets: dict[str, dict[str, Any]] = {}
    slots: dict[tuple[str, str, str, str], list[Any]] = {}  # [lock, requests using it]: one turn at a time per slot

    def prune(now: float) -> None:
        for store in (calls_per_root, sessions):
            for k in [k for k, (_, exp) in list(store.items()) if isinstance(exp, float) and exp < now]:
                store.pop(k, None)
        for k in [k for k, t in list(tickets.items()) if t["expires"] < now]:
            tickets.pop(k, None)

    @contextlib.asynccontextmanager
    async def slot(key: tuple[str, str, str, str]):
        entry = slots.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0 and slots.get(key) is entry:
                slots.pop(key, None)  # nobody holds or waits for it: safe to drop

    def cancel_tickets(session: str) -> None:
        """A session that moved on (a new turn, a rejection) makes its old tickets meaningless: void them."""
        for k in [k for k, t in list(tickets.items()) if t["session"] == session]:
            tickets.pop(k, None)

    def caller_of(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if len(token) >= 32:
            import hmac
            for agent, a in assignments().items():
                if a.get("token") and hmac.compare_digest(str(a["token"]), token):
                    return agent
        raise HTTPException(401, "unknown agent")

    def peers_of(agent: str) -> list[str]:
        data = registry()
        entry = data.get("sample") if agent == "sample" else next(
            (a for a in data.get("agents", []) if isinstance(a, dict) and a.get("name") == agent), None)
        return [p for p in (entry or {}).get("peers") or [] if isinstance(p, str)]

    def exists(agent: str) -> bool:
        if agent == "sample":
            return True
        return any(isinstance(a, dict) and a.get("name") == agent and a.get("internal") for a in registry().get("agents", []))

    async def send(agent: str, method: str, path: str, user: str, delegation: str, payload: dict | None,
                   request: Request, timeout: float) -> httpx.Response:
        body = json.dumps(payload).encode() if payload is not None else b""
        headers = {"content-type": "application/json", "x-forwarded-email": user, DELEGATION_HEADER: delegation}
        if request.headers.get("traceparent"):
            headers["traceparent"] = request.headers["traceparent"]
        outgoing = http.build_request(method, upstream(agent) + path, content=body, headers=headers,
                                      timeout=httpx.Timeout(10.0, read=timeout))
        outgoing.headers.update(signed_headers(secret, agent, method, outgoing.url.raw_path.decode("ascii"), user,
                                               delegation, body))
        return await http.send(outgoing)

    def authorize(request: Request, callee: str) -> tuple[str, dict[str, Any], list[str]]:
        caller = caller_of(request)
        claims = verify(key, request.headers.get(DELEGATION_HEADER))
        if claims is None:
            raise HTTPException(401, "this call isn't part of a request the platform issued (or it expired)")
        if claims["a"] != caller:
            raise HTTPException(403, "the delegation was issued to a different agent")
        if not _NAME.fullmatch(callee) or not exists(callee):
            raise HTTPException(404, f"no agent named {callee} on this server")
        if callee not in peers_of(caller):
            raise HTTPException(403, f"{caller} isn't allowed to call {callee} (an admin sets this on the Agents page)")
        chain = [*claims["c"], caller]
        if callee in chain:
            raise HTTPException(409, f"refused: {callee} is already in this chain ({' > '.join(chain)}), which would loop")
        if len(chain) > MAX_DEPTH:
            raise HTTPException(409, f"refused: chains of agents stop at {MAX_DEPTH} deep")
        return caller, claims, chain

    def count(claims: dict[str, Any]) -> None:  # turns and decisions both count
        root = str(claims.get("r") or "")
        used, _ = calls_per_root.get(root, (0, 0.0))
        if used >= MAX_CALLS:
            raise HTTPException(429, f"refused: one request can make at most {MAX_CALLS} agent calls")
        calls_per_root[root] = (used + 1, float(claims["x"]))

    def result(callee: str, caller: str, claims: dict, chain: list[str], sid: str, body: dict[str, Any],
               slot_key: tuple[str, str, str, str]) -> dict[str, Any]:
        out = {"agent": callee, "status": body.get("status", "completed"), "reply": str(body.get("reply") or ""),
               "blocked": body.get("blocked"), "chain": [*chain, callee]}
        approvals = [a for a in body.get("approvals") or [] if isinstance(a, dict)]
        if out["status"] == "approval_required" and approvals:
            ticket = secrets.token_urlsafe(18)
            actions = [{"tool": str(a.get("tool")), "arguments": a.get("arguments") or {}} for a in approvals]
            tickets[ticket] = {"user": claims["u"], "caller": caller, "callee": callee, "session": sid, "slot": slot_key,
                               "items": [{"id": str(a.get("id")), **act} for a, act in zip(approvals, actions)],
                               "actions": actions, "expires": time.time() + TICKET_SECONDS}
            out.update(ticket=ticket, actions=actions)
        return out

    async def chat(callee: str, user: str, delegation: str, message: str, sid: str | None, request: Request,
                   timeout: float) -> httpx.Response:
        response = await send(callee, "POST", "/v1/chat", user, delegation,
                              {"message": message, **({"session_id": sid} if sid else {})}, request, timeout)
        if response.status_code == 409 and sid:
            # The person never approved what the other agent asked last time: reject it (and anything it asks
            # next while being told no), void the old tickets, then continue with the new request.
            cancel_tickets(sid)
            detail = (response.json().get("detail") or {}) if response.headers.get("content-type", "").startswith("application/json") else {}
            pending = detail.get("approvals") if isinstance(detail, dict) else None
            for _ in range(5):
                if not pending:
                    break
                rejected = await send(callee, "POST", f"/v1/sessions/{sid}/approvals", user, delegation,
                                      {"decisions": [{"id": p["id"], "approved": False, "comment": "not approved by the person"}
                                                     for p in pending]}, request, timeout)
                body = rejected.json() if rejected.headers.get("content-type", "").startswith("application/json") else {}
                pending = body.get("approvals") if isinstance(body, dict) and body.get("status") == "approval_required" else None
            response = await send(callee, "POST", "/v1/chat", user, delegation,
                                  {"message": message, "session_id": sid}, request, timeout)
        return response

    def answer(response: httpx.Response, callee: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 200 and isinstance(body, dict):
            return body
        if response.status_code == 401:
            raise HTTPException(502, f"{callee} refused the platform's signature (restart it: docker compose up -d)")
        detail = body.get("detail") if isinstance(body, dict) else None
        raise HTTPException(502, f"{callee} answered HTTP {response.status_code}" + (f": {detail}" if isinstance(detail, str) else ""))

    @app.post("/agents/{callee}/turn")
    async def turn(callee: str, request: Request) -> JSONResponse:
        started = time.time()
        prune(started)
        caller, claims, chain = authorize(request, callee)
        body = _strict_json(await request.body())
        message = body.get("message")
        conversation = body.get("conversation") or claims.get("r") or ""
        if not isinstance(message, str) or not 1 <= len(message) <= 20_000:
            raise HTTPException(422, "message: 1 to 20000 characters")
        if not isinstance(conversation, str) or not _CONVERSATION.fullmatch(conversation):
            raise HTTPException(422, "conversation: an id")
        user = claims["u"]
        try:
            count(claims)
        except HTTPException as exc:
            log.add(user=user, caller=caller, callee=callee, chain=[*chain, callee], outcome="refused", detail=exc.detail)
            raise
        delegation = mint(key, user=user, audience=callee, chain=chain, root=str(claims.get("r")), expires=claims["x"],
                          kind="turn")  # the other agent can call onward, but can't decide anyone's approvals with it
        timeout = max(5.0, min(CALL_TIMEOUT, claims["x"] - started))
        slot_key = (user, caller, conversation, callee)
        async with slot(slot_key):  # one turn at a time per conversation with the other agent
            sid = sessions.get(slot_key, (None, 0.0))[0]
            if sid:
                cancel_tickets(sid)  # a new turn: approvals asked before it no longer stand
            try:
                response = await chat(callee, user, delegation, message, sid, request, timeout)
                if response.status_code == 404 and sid:  # that conversation expired over there: start a new one
                    sessions.pop(slot_key, None)
                    sid = None
                    response = await chat(callee, user, delegation, message, None, request, timeout)
                data = answer(response, callee)
            except httpx.HTTPError:
                log.add(user=user, caller=caller, callee=callee, chain=[*chain, callee], outcome="error",
                        detail=f"{callee} isn't answering", ms=int((time.time() - started) * 1000))
                raise HTTPException(502, f"{callee} isn't answering right now") from None
            except HTTPException as exc:
                log.add(user=user, caller=caller, callee=callee, chain=[*chain, callee], outcome="error",
                        detail=str(exc.detail), ms=int((time.time() - started) * 1000))
                raise
            sid = str(data.get("session_id") or "")
            if sid:
                sessions[slot_key] = (sid, time.time() + SESSION_SECONDS)
            out = result(callee, caller, claims, chain, sid, data, slot_key)
        outcome = "approval_requested" if out.get("ticket") else ("blocked" if out.get("blocked") else "answered")
        log.add(user=user, caller=caller, callee=callee, chain=out["chain"], outcome=outcome,
                detail=", ".join(a["tool"] for a in out.get("actions", [])) or None, ms=int((time.time() - started) * 1000))
        return JSONResponse(out)

    @app.post("/agents/{callee}/decide")
    async def decide(callee: str, request: Request) -> JSONResponse:
        started = time.time()
        prune(started)
        caller, claims, chain = authorize(request, callee)
        if claims.get("k") != "decide":
            # Only while the person is submitting an approval to the calling agent (the router marks that request):
            # an agent can't approve anything on its own, whenever it likes.
            raise HTTPException(403, "decisions only go through while the person is approving in their chat")
        body = _strict_json(await request.body())
        ticket_id, approved, actions = body.get("ticket"), body.get("approved"), body.get("actions")
        if not isinstance(ticket_id, str) or not isinstance(approved, bool) or not isinstance(actions, list):
            raise HTTPException(422, "send ticket, approved and actions")
        ticket = tickets.get(ticket_id)
        user = claims["u"]
        if ticket is None or ticket["user"] != user or ticket["caller"] != caller or ticket["callee"] != callee:
            raise HTTPException(404, "no such approval ticket (it may have expired or been used)")
        if _canonical(actions) != _canonical(ticket["actions"]):
            log.add(user=user, caller=caller, callee=callee, chain=[*chain, callee], outcome="refused",
                    detail="approved actions didn't match the request")
            raise HTTPException(409, f"what was approved doesn't match what {callee} asked for; nothing was done")
        count(claims)
        delegation = mint(key, user=user, audience=callee, chain=chain, root=str(claims.get("r")), expires=claims["x"],
                          kind="turn")
        timeout = max(5.0, min(CALL_TIMEOUT, claims["x"] - started))
        async with slot(ticket["slot"]):
            if tickets.pop(ticket_id, None) is None:  # single use, even under concurrency
                raise HTTPException(404, "no such approval ticket (it may have expired or been used)")
            try:
                # What the other agent has pending *now* must be exactly what the person approved: same ids, same
                # tools, same arguments. Anything else (it moved on, it asks for something new) and nothing happens.
                current = await send(callee, "GET", f"/v1/sessions/{ticket['session']}/approvals", user, delegation,
                                     None, request, timeout)
                pending = current.json() if current.status_code == 200 else None
                expected = sorted(_canonical(i) for i in ticket["items"])
                actual = sorted(_canonical({"id": str(p.get("id")), "tool": str(p.get("tool")),
                                            "arguments": p.get("arguments") or {}})
                                for p in pending or [] if isinstance(p, dict))
                if pending is None or actual != expected:
                    log.add(user=user, caller=caller, callee=callee, chain=[*chain, callee], outcome="refused",
                            detail="the other agent's pending approval changed")
                    raise HTTPException(409, f"{callee} is no longer waiting for exactly this; nothing was done")
                response = await send(callee, "POST", f"/v1/sessions/{ticket['session']}/approvals", user, delegation,
                                      {"decisions": [{"id": i["id"], "approved": approved,
                                                      "comment": f"decided by {user} in {caller}"} for i in ticket["items"]]},
                                      request, timeout)
                data = answer(response, callee)
            except httpx.HTTPError:
                raise HTTPException(502, f"{callee} isn't answering right now") from None
            out = result(callee, caller, claims, chain, ticket["session"], data, ticket["slot"])
        log.add(user=user, caller=caller, callee=callee, chain=out["chain"],
                outcome="approved" if approved else "rejected", detail=", ".join(a["tool"] for a in ticket["actions"]),
                ms=int((time.time() - started) * 1000))
        return JSONResponse(out)

    @app.get("/read/{agent}/{path:path}")
    async def fleet_read(agent: str, path: str, request: Request) -> Response:
        """The fleet view's read-only window onto signed agents: probes, overview and eval report only."""
        peer = request.client.host if request.client else ""
        if not await is_fleet(peer):
            raise HTTPException(403, "only the fleet view reads through here")
        if path not in FLEET_PATHS or not (_NAME.fullmatch(agent) or agent == "sample") or not exists(agent) \
                or request.url.query:
            raise HTTPException(404, "not readable")
        try:
            response = await send(agent, "GET", "/" + path, FLEET_CALLER, "", None, request, 15.0)
        except httpx.HTTPError:
            raise HTTPException(502, f"{agent} isn't answering") from None
        return Response(response.content, status_code=response.status_code,
                        media_type=response.headers.get("content-type", "application/json"))

    app.state.agent_calls = {"tickets": tickets, "sessions": sessions, "calls_per_root": calls_per_root}


def new_root() -> str:
    return uuid.uuid4().hex
