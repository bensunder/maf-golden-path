"""Microsoft Teams channel (M365 Agents SDK) on top of the agentkit conversation service.

    from agentkit.channels import TeamsChannel
    app = create_app(create_agent, channels=[TeamsChannel()])

What it handles so a team doesn't have to:

* ``POST /api/messages`` with Bot Connector JWT validation (only that route: the rest of the app keeps
  Easy Auth), anti-SSRF checks on ``serviceUrl``, managed-identity auth to reply.
* Answers inside Teams' ~15 s window: the turn runs in the background and replies proactively.
* One agentkit session per (Teams conversation, user), owned by the user's Entra object id, stored in
  the same shared store as the HTTP API (so it works across replicas).
* Approvals as Adaptive Cards with ``Action.Execute``: posted to an approvers channel under
  separation of duties, or to the requester for confirmation. Clicks go through the same checks as
  ``POST /v1/sessions/{id}/approvals`` (approver directory, not-your-own-request, audit), the card is
  replaced so it can't be clicked twice, and the result is posted back to the requester.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Coroutine, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentkit.hosting import (
    AgentKitSettings,
    ApprovalPending,
    Caller,
    ConversationError,
    ConversationService,
    Decision,
    NotAllowed,
    SessionLockTimeout,
    SessionRecord,
    TurnResult,
)

from .cards import VERB_APPROVE, VERB_REJECT, VERB_SUBMIT, approval_card, decided_card, notice_card

__all__ = [
    "ApproverDirectory",
    "GraphGroupApprovers",
    "StaticApprovers",
    "TeamsChannel",
    "TeamsSettings",
    "TeamsUser",
    "teams_from_env",
    "teams_session_id",
]

logger = logging.getLogger(__name__)

CONVERSATION_KEY = "teams.conversation"  # SessionRecord.meta: where to post replies for this session
_APPROVALS_CHANNEL_KEY = "teams-approvals-channel"
_RESET_COMMANDS = {"reset", "/reset", "new", "/new", "start over"}


# ---------------------------------------------------------------------------- settings
class TeamsSettings(BaseSettings):
    """``AGENTKIT_TEAMS_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="AGENTKIT_TEAMS_", env_file=".env", extra="ignore")

    #: Client id of the bot identity (the user-assigned managed identity registered on the Azure Bot).
    app_id: str | None = None
    tenant_id: str | None = None
    #: ``managed_identity`` in Azure. ``client_secret`` for a dev tunnel. ``anonymous`` only for local
    #: and test (Agents Playground, offline tests): inbound JWTs are not checked.
    auth_type: Literal["managed_identity", "client_secret", "anonymous"] = "managed_identity"
    client_secret: SecretStr | None = None
    route: str = "/api/messages"
    #: Comma-separated Entra object ids (or UPNs) allowed to approve in Teams.
    approvers: str = ""
    #: Entra group whose (transitive) members may approve; checked via Microsoft Graph with the bot identity.
    approver_group_id: str | None = None
    #: Teams channel id (``19:...@thread.tacv2``) that receives approval cards. Mention the agent there
    #: once (or install it to the team) so it can post. Without it, cards go to the requester's chat.
    approvals_channel_id: str | None = None
    #: Extra hosts allowed as ``serviceUrl`` besides Microsoft's (tests use a local fake connector).
    allowed_service_hosts: str = ""
    welcome: str | None = None

    @model_validator(mode="after")
    def _check(self) -> TeamsSettings:
        if self.auth_type != "anonymous" and not self.app_id:
            raise ValueError("AGENTKIT_TEAMS_APP_ID is required unless auth_type is 'anonymous'")
        if self.auth_type == "client_secret" and not self.client_secret:
            raise ValueError("auth_type 'client_secret' needs AGENTKIT_TEAMS_CLIENT_SECRET")
        return self

    @staticmethod
    def _split(value: str) -> list[str]:
        return [v.strip() for v in value.split(",") if v.strip()]

    @property
    def approver_ids(self) -> list[str]:
        return self._split(self.approvers)

    @property
    def service_hosts(self) -> list[str]:
        return self._split(self.allowed_service_hosts)


class _TeamsProbe(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTKIT_TEAMS_", env_file=".env", extra="ignore")
    app_id: str | None = None
    auth_type: str = "managed_identity"


def teams_from_env(**kwargs: Any) -> list[TeamsChannel]:
    """``[TeamsChannel(...)]`` when Teams is configured (``AGENTKIT_TEAMS_APP_ID``, set by the service's
    Bicep, or ``AGENTKIT_TEAMS_AUTH_TYPE=anonymous`` for local tools), else ``[]``: the same code runs on
    a laptop without a bot registration."""
    probe = _TeamsProbe()
    if not probe.app_id and probe.auth_type != "anonymous":
        logger.info("Teams channel off: AGENTKIT_TEAMS_APP_ID is not set")
        return []
    return [TeamsChannel(**kwargs)]


# ---------------------------------------------------------------------------- approvers
@dataclass(frozen=True)
class TeamsUser:
    id: str  # Teams user id (29:...)
    aad_object_id: str | None
    name: str | None
    tenant_id: str | None


class ApproverDirectory(Protocol):
    async def is_approver(self, user: TeamsUser) -> bool: ...


class StaticApprovers:
    """A fixed list of Entra object ids (or UPNs). Fine for small teams; prefer a group."""

    def __init__(self, ids: Iterable[str]):
        self._ids = {i.strip().lower() for i in ids if i.strip()}

    async def is_approver(self, user: TeamsUser) -> bool:
        return bool(user.aad_object_id) and user.aad_object_id.lower() in self._ids


class GraphGroupApprovers:
    """Members (including nested) of an Entra group, checked with Microsoft Graph ``checkMemberGroups``.

    The bot's managed identity needs the ``GroupMember.Read.All`` application permission. Answers are
    cached for ``ttl`` seconds; any error counts as *not* an approver (fail closed)."""

    def __init__(self, group_id: str, *, client: Any = None, ttl: float = 300.0):
        from agentkit.tools import ApiClient, ManagedIdentityAuth

        self.group_id = group_id
        self.client = client or ApiClient(
            "https://graph.microsoft.com/v1.0", auth=ManagedIdentityAuth("https://graph.microsoft.com/.default")
        )
        self._ttl = ttl
        self._cache: dict[str, tuple[float, bool]] = {}

    async def is_approver(self, user: TeamsUser) -> bool:
        oid = user.aad_object_id
        if not oid:
            return False
        hit = self._cache.get(oid)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            result = await self.client.request("POST", f"/users/{oid}/checkMemberGroups",
                                               json={"groupIds": [self.group_id]})
            allowed = self.group_id in (result or {}).get("value", [])
        except Exception as exc:  # fail closed, don't cache
            logger.warning("approver check for %s failed: %s", oid, exc)
            return False
        self._cache[oid] = (time.monotonic() + self._ttl, allowed)
        return allowed


class _AnyOf:
    def __init__(self, *directories: ApproverDirectory):
        self._directories = directories

    async def is_approver(self, user: TeamsUser) -> bool:
        for directory in self._directories:
            if await directory.is_approver(user):
                return True
        return False


# ---------------------------------------------------------------------------- helpers
def teams_session_id(conversation_id: str, user_id: str) -> str:
    """One session per (Teams conversation, user): in a channel thread or group chat each person gets
    their own agent session, so ownership checks hold."""
    return "teams-" + hashlib.sha256(f"{conversation_id}|{user_id}".encode()).hexdigest()[:40]


def _channel_data(activity: Any) -> dict[str, Any]:
    data = activity.channel_data
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    return data.model_dump(by_alias=True, exclude_none=True) if hasattr(data, "model_dump") else dict(data)


def _sources_markdown(citations: list[Any]) -> str:
    """'Sources: [1] Title' with links for http(s) URLs only; titles escaped (they come from documents)."""
    from .cards import plain

    parts = []
    for c in citations:
        title = plain(c.title or c.id, 120)
        url = c.url or ""
        if url.startswith(("https://", "http://")) and not any(ch in url for ch in " ()<>"):
            parts.append(f"[{c.n}] [{title}]({url})")
        else:
            parts.append(f"[{c.n}] {title}")
    return "Sources: " + " · ".join(parts)


class _NullStorage:
    """The SDK insists on a Storage for its turn state. agentkit keeps state in its own session store,
    so this reads nothing and writes nothing (a MemoryStorage would grow per user and per replica)."""

    async def read(self, keys, *, target_cls=None, **kwargs):
        return {}

    async def write(self, changes):
        return None

    async def delete(self, keys):
        return None


# ---------------------------------------------------------------------------- channel
class TeamsChannel:
    def __init__(
        self,
        teams_settings: TeamsSettings | None = None,
        *,
        approvers: ApproverDirectory | None = None,
        agent_name: str | None = None,
    ):
        self.teams = teams_settings or TeamsSettings()
        directories: list[ApproverDirectory] = []
        if approvers is not None:
            directories.append(approvers)
        if self.teams.approver_ids:
            directories.append(StaticApprovers(self.teams.approver_ids))
        if self.teams.approver_group_id:
            directories.append(GraphGroupApprovers(self.teams.approver_group_id))
        self.approvers: ApproverDirectory | None = _AnyOf(*directories) if directories else None
        self._agent_name = agent_name
        self._tasks: set[asyncio.Task] = set()
        self.service: ConversationService | None = None
        self.settings: AgentKitSettings | None = None
        self.adapter: Any = None
        self.application: Any = None

    # ------------------------------------------------------------ wiring
    def _check_policy(self, settings: AgentKitSettings) -> None:
        problems = []
        if self.teams.auth_type == "anonymous" and settings.environment not in ("local", "test"):
            problems.append("Teams auth_type 'anonymous' is only allowed in local and test")
        if settings.environment == "prod" and self.teams.auth_type != "managed_identity":
            problems.append("Teams auth_type must be 'managed_identity' in prod")
        if settings.approver_role and self.approvers is None:
            problems.append("approvals need an approver directory in Teams: set AGENTKIT_TEAMS_APPROVERS or "
                            "AGENTKIT_TEAMS_APPROVER_GROUP_ID (Easy Auth roles don't reach the bot endpoint)")
        if problems:
            raise ValueError("; ".join(problems))

    def _auth_configuration(self):
        from microsoft_agents.hosting.core import AgentAuthConfiguration, AuthTypes

        t = self.teams
        if t.auth_type == "anonymous":
            return AgentAuthConfiguration(anonymous_allowed=True)
        if t.auth_type == "client_secret":
            return AgentAuthConfiguration(auth_type=AuthTypes.client_secret, client_id=t.app_id,
                                          tenant_id=t.tenant_id, client_secret=t.client_secret.get_secret_value())
        return AgentAuthConfiguration(auth_type=AuthTypes.user_managed_identity, client_id=t.app_id,
                                      tenant_id=t.tenant_id)

    def install(self, app: Any, service: ConversationService, settings: AgentKitSettings) -> None:
        from microsoft_agents.authentication.msal import MsalConnectionManager
        from microsoft_agents.hosting.core import AgentApplication, ApplicationOptions, OutboundHostValidator
        from microsoft_agents.hosting.fastapi import CloudAdapter, jwt_authorization_decorator

        self._check_policy(settings)
        self.service, self.settings = service, settings
        self._agent_name = self._agent_name or settings.service_name
        auth_config = self._auth_configuration()
        connections = MsalConnectionManager(connections_configurations={"SERVICE_CONNECTION": auth_config})
        self.adapter = CloudAdapter(
            connection_manager=connections,
            host_validator=OutboundHostValidator(enabled=True, hosts=self.teams.service_hosts),
        )
        self.application = AgentApplication(
            ApplicationOptions(storage=_NullStorage(), adapter=self.adapter, bot_app_id=self.teams.app_id or "",
                               start_typing_timer=False),
            connection_manager=connections,
        )
        self._register_routes(self.application)
        # JWT validation on this route only; the SDK reads its config from app.state.
        app.state.agent_configuration = auth_config

        @jwt_authorization_decorator
        async def messages(request):
            return await self.adapter.process(request, self.application)

        # A plain Starlette route: the SDK handles the body; nothing for FastAPI to validate or document.
        app.router.add_route(self.teams.route, messages, methods=["POST"], include_in_schema=False)

    async def shutdown(self) -> None:
        await self.drain()

    async def drain(self, timeout: float | None = 30) -> None:
        """Wait for background turns (tests; graceful shutdown)."""
        while self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)
            if timeout is not None:
                break

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("Teams background task failed", exc_info=t.exception())

        task.add_done_callback(done)

    # ------------------------------------------------------------ identity
    @staticmethod
    def _user(activity: Any) -> TeamsUser:
        account = activity.from_property
        tenant = (activity.conversation.tenant_id if activity.conversation else None) or (
            _channel_data(activity).get("tenant", {}).get("id")
        )
        return TeamsUser(id=account.id, aad_object_id=getattr(account, "aad_object_id", None), name=account.name,
                         tenant_id=tenant)

    async def _caller(self, activity: Any, *, check_approver: bool = False) -> Caller:
        user = self._user(activity)
        is_approver = bool(check_approver and self.approvers and await self.approvers.is_approver(user))
        return Caller(user_id=user.aad_object_id or user.id, tenant_id=user.tenant_id, is_approver=is_approver,
                      display_name=user.name, channel="teams")

    # ------------------------------------------------------------ routes
    def _register_routes(self, application: Any) -> None:
        import re

        from microsoft_agents.hosting.core.app.adaptive_card import factory

        @application.before_turn
        async def remember_approvals_channel(context, state) -> bool:
            await self._maybe_capture_approvals_channel(context)
            return True

        @application.conversation_update("membersAdded")
        async def welcome(context, state) -> None:
            recipient = context.activity.recipient.id if context.activity.recipient else None
            added = [m.id for m in (context.activity.members_added or [])]
            if self.teams.welcome and any(m != recipient for m in added):
                await context.send_activity(self.teams.welcome)

        @application.adaptive_card.action_execute(re.compile(r"^agentkit\.approvals\."))
        async def on_decision(context, state, data):
            return await self._on_decision(context, data, factory)

        @application.activity("message")
        async def on_message(context, state) -> None:
            await self._on_message(context)

    # ------------------------------------------------------------ messages
    async def _on_message(self, context: Any) -> None:
        from microsoft_agents.activity import Activity
        from microsoft_agents.hosting.core.app.proactive import Conversation

        activity = context.activity
        text = (activity.text or "").strip()
        if not text:
            return  # attachments only, card submits etc.
        caller = await self._caller(activity)
        session_id = teams_session_id(activity.conversation.id, caller.user_id or "")
        if text.lower() in _RESET_COMMANDS:
            try:
                await self.service.delete(caller, session_id)
                await context.send_activity("Started a new conversation.")
            except ConversationError as exc:
                await context.send_activity(str(exc.detail))
            return
        conversation = Conversation.from_turn_context(context).store_item_to_json()
        await context.send_activity(Activity(type="typing"))
        self._spawn(self._run_turn(caller, text, session_id, conversation))

    async def _run_turn(self, caller: Caller, text: str, session_id: str, conversation: dict[str, Any]) -> None:
        try:
            result = await self.service.run_turn(caller, text, session_id, create=True,
                                                 meta={CONVERSATION_KEY: conversation})
        except ApprovalPending as exc:
            tools = ", ".join(sorted({p["tool"] for p in exc.pending}))
            await self._send_text(conversation, f"I'm waiting for a decision on {tools} before I can continue. "
                                                "Say 'reset' to start over.")
            return
        except SessionLockTimeout:
            await self._send_text(conversation, "I'm still working on your previous message. Try again in a moment.")
            return
        except ConversationError as exc:
            await self._send_text(conversation, str(exc.detail))
            return
        except Exception:
            logger.exception("Teams turn failed")
            await self._send_text(conversation, "Sorry, something went wrong. Please try again.")
            return
        await self._deliver(result, conversation, requested_by=caller.display_name)

    async def _deliver(self, result: TurnResult, conversation: dict[str, Any], *, requested_by: str | None,
                       prefix: str = "") -> None:
        text = (prefix + result.reply).strip()
        if text and result.citations:
            text += "\n\n" + _sources_markdown(result.citations)
        if text:
            await self._send_text(conversation, text)
        if result.pending:
            await self._request_approval(result, conversation, requested_by)

    async def _request_approval(self, result: TurnResult, conversation: dict[str, Any], requested_by: str | None) -> None:
        separation = bool(self.settings.approver_role)
        card = approval_card(result.session_id, result.pending, requested_by=requested_by,
                             agent_name=self._agent_name, separation=separation)
        target = conversation
        if separation and self.teams.approvals_channel_id:
            record = await self.service.store.get(_APPROVALS_CHANNEL_KEY)
            if record and record.meta.get(CONVERSATION_KEY):
                target = record.meta[CONVERSATION_KEY]
                await self._send_text(conversation, "I've sent this to an approver. I'll post the result here.")
            else:
                logger.warning("approvals channel %s not registered yet: mention the agent there once; "
                               "posting the card in the requester's conversation", self.teams.approvals_channel_id)
        await self._send_card(target, card)

    # ------------------------------------------------------------ decisions
    async def _on_decision(self, context: Any, data: Any, factory: Any):
        activity = context.activity
        verb = ((activity.value or {}).get("action") or {}).get("verb") if isinstance(activity.value, dict) else None
        data = data if isinstance(data, Mapping) else {}
        session_id, ids = data.get("session_id"), data.get("approval_ids")
        if not isinstance(session_id, str) or not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            return factory.bad_request("malformed approval action")
        comment = data.get("comment") if isinstance(data.get("comment"), str) else None
        comment = (comment or "").strip()[:1000] or None
        if verb == VERB_APPROVE:
            verdicts = {i: True for i in ids}
        elif verb == VERB_REJECT:
            verdicts = {i: False for i in ids}
        elif verb == VERB_SUBMIT:
            verdicts = {i: data.get(f"decision_{i}", "approve") == "approve" for i in ids}
        else:
            return factory.bad_request("unknown action")
        decisions = {i: Decision(approved=v, comment=comment) for i, v in verdicts.items()}

        caller = await self._caller(activity, check_approver=True)
        try:
            record = await self.service.precheck_decision(caller, session_id, decisions)
        except NotAllowed as exc:
            return factory.message(str(exc.detail))  # shown to the clicker only; the card stays
        except ConversationError:
            return factory.adaptive_card(notice_card("This request was already decided or has expired."))
        pending = [p for p in record.meta.get("approvals", []) if p["id"] in verdicts]
        self._spawn(self._resume(caller, session_id, decisions, record))
        return factory.adaptive_card(decided_card(pending, verdicts, decided_by=caller.display_name,
                                                  comment=comment, decided_at=time.time()))

    async def _resume(self, caller: Caller, session_id: str, decisions: Mapping[str, Decision],
                      record: SessionRecord) -> None:
        conversation = record.meta.get(CONVERSATION_KEY)
        approved = all(d.approved for d in decisions.values())
        verdict = "Approved" if approved else ("Rejected" if not any(d.approved for d in decisions.values())
                                               else "Decided")
        try:
            result = await self.service.decide(caller, session_id, decisions)
        except ConversationError as exc:  # lost a race (someone decided first, session expired)
            logger.info("decision on %s not applied: %s", session_id, exc)
            if conversation:
                await self._send_text(conversation, f"A decision couldn't be applied: {exc.detail}")
            return
        except Exception:
            logger.exception("resuming %s after approval failed", session_id)
            if conversation:
                await self._send_text(conversation, "Sorry, something went wrong after the decision.")
            return
        if conversation:
            await self._deliver(result, conversation, requested_by=None,
                                prefix=f"{verdict} by {caller.display_name or 'an approver'}. ")

    # ------------------------------------------------------------ approvals channel
    async def _maybe_capture_approvals_channel(self, context: Any) -> None:
        channel_id = self.teams.approvals_channel_id
        if not channel_id:
            return
        from microsoft_agents.hosting.core.app.proactive import Conversation

        data = _channel_data(context.activity)
        if (data.get("channel") or {}).get("id") != channel_id:
            return
        conversation = Conversation.from_turn_context(context)
        reference = conversation.conversation_reference
        reference.conversation.id = channel_id  # post new threads in the channel, not replies
        reference.activity_id = None
        existing = await self.service.store.get(_APPROVALS_CHANNEL_KEY)
        stored = conversation.store_item_to_json()
        if existing and existing.meta.get(CONVERSATION_KEY) == stored and existing.expires_at > time.time() + 86_400:
            return
        await self.service.store.put(_APPROVALS_CHANNEL_KEY, SessionRecord(
            owner=None, data={}, expires_at=time.time() + 365 * 86_400, meta={CONVERSATION_KEY: stored}))
        logger.info("registered Teams approvals channel %s", channel_id)

    # ------------------------------------------------------------ sending
    async def _send(self, conversation: dict[str, Any], activity: Any) -> None:
        from microsoft_agents.hosting.core.app.proactive import Conversation

        conv = Conversation.from_json_to_store_item(conversation)
        claims = Conversation.identity_from_claims(conv.claims)
        continuation = conv.conversation_reference.get_continuation_activity()

        async def callback(context):
            # The continuation event gets a random id that send_activity would copy into replyToId (a reply
            # to a message that doesn't exist). Proactive posts go to the conversation itself.
            context.activity.id = None
            await context.send_activity(activity)

        await self.adapter.continue_conversation_with_claims(claims, continuation, callback)

    async def _send_text(self, conversation: dict[str, Any], text: str) -> None:
        from microsoft_agents.hosting.core import MessageFactory

        await self._send(conversation, MessageFactory.text(text))

    async def _send_card(self, conversation: dict[str, Any], card: dict[str, Any]) -> None:
        from microsoft_agents.hosting.core import CardFactory, MessageFactory

        await self._send(conversation, MessageFactory.attachment(CardFactory.adaptive_card(card)))
