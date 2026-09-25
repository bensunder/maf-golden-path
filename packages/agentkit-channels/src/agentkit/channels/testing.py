"""Offline Teams tests: send real Bot Framework activities to your app and see what it sends back.

    from agentkit.channels.testing import TeamsTestClient, TeamsTestUser

    async def test_refund_needs_approval(app_with_teams):
        alice, bob = TeamsTestUser("alice"), TeamsTestUser("bob")
        async with TeamsTestClient(app_with_teams) as teams:
            await teams.send("Refund $129 on A1002", user=alice)
            card = teams.last_card()
            await teams.click(card, "approve", user=bob)
            assert "Approved by Bob" in teams.texts()[-1]

A ``FakeConnector`` (a local stand-in for the Bot Connector service) records every activity the agent
posts; activities go in through the real ``/api/messages`` route, JWT/anonymous handling and SDK.
"""

from __future__ import annotations

import itertools
import socket
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = ["FakeConnector", "TeamsTestClient", "TeamsTestUser", "teams_test_settings"]


@dataclass(frozen=True)
class TeamsTestUser:
    handle: str
    name: str | None = None
    aad_object_id: str | None = None
    tenant_id: str = "tenant-test"

    @property
    def display_name(self) -> str:
        return self.name or self.handle.capitalize()

    @property
    def object_id(self) -> str:
        return self.aad_object_id or f"oid-{self.handle}"

    def account(self) -> dict[str, Any]:
        return {"id": f"29:{self.handle}", "name": self.display_name, "aadObjectId": self.object_id}


def teams_test_settings(**overrides: Any):
    """TeamsSettings for offline tests: anonymous inbound auth, local fake connector allowed."""
    from .teams import TeamsSettings

    values = {"auth_type": "anonymous", "allowed_service_hosts": "127.0.0.1,localhost", **overrides}
    return TeamsSettings(_env_file=None, **values)


class FakeConnector:
    """Records activities the agent sends (``POST /v3/conversations/{id}/activities[/{replyTo}]``)."""

    def __init__(self) -> None:
        self.activities: list[dict[str, Any]] = []
        self.url: str | None = None
        self._runner = None
        self._ids = itertools.count(1)

    async def start(self) -> str:
        from aiohttp import web

        async def post_activity(request: web.Request) -> web.Response:
            body = await request.json()
            body["_conversation_id"] = request.match_info["conversation_id"]
            body["_authorization"] = request.headers.get("Authorization")
            self.activities.append(body)
            return web.json_response({"id": f"activity-{next(self._ids)}"})

        async def update_activity(request: web.Request) -> web.Response:
            body = await request.json()
            body["_conversation_id"] = request.match_info["conversation_id"]
            body["_updated"] = request.match_info["activity_id"]
            self.activities.append(body)
            return web.json_response({"id": request.match_info["activity_id"]})

        app = web.Application()
        app.router.add_post("/v3/conversations/{conversation_id}/activities", post_activity)
        app.router.add_post("/v3/conversations/{conversation_id}/activities/{activity_id}", post_activity)
        app.router.add_put("/v3/conversations/{conversation_id}/activities/{activity_id}", update_activity)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", port).start()
        self.url = f"http://127.0.0.1:{port}/"
        return self.url

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------ what was sent
    def messages(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        return [a for a in self.activities if a.get("type") == "message"
                and (conversation_id is None or a["_conversation_id"] == conversation_id)]

    def texts(self, conversation_id: str | None = None) -> list[str]:
        return [a["text"] for a in self.messages(conversation_id) if a.get("text")]

    def cards(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        cards = []
        for activity in self.messages(conversation_id):
            for attachment in activity.get("attachments") or []:
                if attachment.get("contentType") == "application/vnd.microsoft.card.adaptive":
                    cards.append({**attachment["content"], "_conversation_id": activity["_conversation_id"]})
        return cards


class TeamsTestClient:
    """Drives an agentkit app (with a ``TeamsChannel``) the way Teams would."""

    def __init__(self, app: Any, *, channel: Any = None, connector: FakeConnector | None = None,
                 route: str | None = None) -> None:
        from .teams import TeamsChannel

        self.app = app
        self.channel = channel or next((c for c in getattr(app.state, "channels", []) if isinstance(c, TeamsChannel)), None)
        if self.channel is None:
            raise ValueError("pass channel=<TeamsChannel> (or build the app with create_app(..., channels=[...]))")
        self.connector = connector or FakeConnector()
        self.route = route or self.channel.teams.route
        self._lifespan = None
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> TeamsTestClient:
        await self.connector.start()
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://agent")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.channel.drain()
        await self._http.aclose()
        await self._lifespan.__aexit__(*exc)
        await self.connector.stop()

    # ------------------------------------------------------------ activities
    def _activity(self, activity_type: str, user: TeamsTestUser, conversation_id: str,
                  conversation_type: str, channel_id: str | None, **extra: Any) -> dict[str, Any]:
        channel_data: dict[str, Any] = {"tenant": {"id": user.tenant_id}}
        if channel_id:
            channel_data["channel"] = {"id": channel_id}
            channel_data["team"] = {"id": channel_id}
        return {
            "type": activity_type,
            "id": str(uuid.uuid4()),
            "timestamp": "2026-09-25T12:00:00Z",
            "serviceUrl": self.connector.url,
            "channelId": "msteams",
            "from": user.account(),
            "conversation": {"id": conversation_id, "conversationType": conversation_type,
                             "tenantId": user.tenant_id, "isGroup": conversation_type != "personal"},
            "recipient": {"id": "28:agent", "name": "Agent"},
            "channelData": channel_data,
            **extra,
        }

    async def post(self, activity: dict[str, Any], *, wait: bool = True) -> httpx.Response:
        response = await self._http.post(self.route, json=activity)
        if wait:
            await self.channel.drain()
        return response

    async def send(self, text: str, *, user: TeamsTestUser, conversation_id: str | None = None,
                   channel_id: str | None = None, wait: bool = True) -> httpx.Response:
        """A user message. Personal chat by default (one conversation per user)."""
        conversation_type = "channel" if channel_id else "personal"
        conversation_id = conversation_id or (f"{channel_id};messageid=1" if channel_id else f"a:{user.handle}")
        return await self.post(self._activity("message", user, conversation_id, conversation_type, channel_id,
                                              text=text), wait=wait)

    async def click(self, card: dict[str, Any], action: str, *, user: TeamsTestUser,
                    inputs: dict[str, Any] | None = None, wait: bool = True) -> dict[str, Any]:
        """Press a card button by title (``"Approve"``) or verb suffix (``"approve"``); returns the invoke
        response body (``{"statusCode", "type", "value"}``). ``inputs`` fills the card's input fields."""
        chosen = None
        for candidate in card.get("actions", []):
            if candidate.get("title", "").lower() == action.lower() or candidate.get("verb", "").endswith("." + action):
                chosen = candidate
        if chosen is None:
            raise ValueError(f"no action {action!r} on card; have {[a.get('title') for a in card.get('actions', [])]}")
        conversation_id = card.get("_conversation_id") or f"a:{user.handle}"
        value = {"action": {"type": "Action.Execute", "verb": chosen["verb"], "data": {**chosen.get("data", {}),
                                                                                        **(inputs or {})}},
                 "trigger": "manual"}
        activity = self._activity("invoke", user, conversation_id, "personal", None, name="adaptiveCard/action",
                                  value=value)
        response = await self.post(activity, wait=wait)
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------ shortcuts
    def texts(self, conversation_id: str | None = None) -> list[str]:
        return self.connector.texts(conversation_id)

    def cards(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        return self.connector.cards(conversation_id)

    def last_card(self, conversation_id: str | None = None) -> dict[str, Any]:
        cards = self.cards(conversation_id)
        if not cards:
            raise AssertionError(f"no card was sent; messages: {self.texts(conversation_id)}")
        return cards[-1]
