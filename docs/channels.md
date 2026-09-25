# Channels: Teams and web chat

Your users probably won't call `POST /v1/chat`. They'll talk to the agent in **Microsoft Teams**, or in a **chat page** in the browser. agentkit ships both as *channels*. A channel only works out who is calling and how to show the result. Everything else runs through the same code path as the JSON API: sessions, locking across replicas, owner checks, human approvals with separation of duties, and the audit log.

```
Teams ──► /api/messages ─┐
Browser ─► /chat, /v1/agui ─┼─► ConversationService ─► your agent (build_agent)
Scripts ─► /v1/chat, approvals ┘   sessions · locks · owner checks · approvals · audit
```

| You get | So you don't have to |
|---|---|
| `TeamsChannel`: Bot Framework endpoint with JWT validation, managed-identity replies, background turns that beat Teams' ~15 s timeout, one session per user and conversation | Learn the M365 Agents SDK, Bot Connector auth, proactive messaging and Teams' timeouts |
| Approvals as **Adaptive Cards**: posted to an approvers channel (separation of duties) or to the requester (confirmation); click-once cards; results posted back to the requester | Build card JSON, `Action.Execute` handlers, approver checks and card updates |
| Approver directory: an Entra **group** (Microsoft Graph, cached, fails closed) or a fixed list | Call Graph and decide what happens when it's down |
| Azure Bot in Bicep (secretless, the service's managed identity), Teams app package script (manifest and icons) | Register a bot by hand, manage a client secret, draw icons |
| `AgUiChannel`: the **AG-UI** protocol, so CopilotKit or any AG-UI client works, with approvals as standard *interrupts* | Design a streaming protocol and a resume flow |
| `WebChat`: a chat page at `/chat` and a dependency-free `<agentkit-chat>` web component | Build and secure a chat UI |
| `agentkit.channels.testing`: offline Teams tests with real activities and a fake Bot Connector | Test a bot by clicking around in Teams |

Estimated saving: **8–14 engineer-days** for a team that needs both Teams and a web chat (see [why-agentkit.md](why-agentkit.md)).

---

## Turn it on

New services: copier asks two questions.

```
enable_web_chat  [true]   chat page at /chat and AG-UI at /v1/agui
enable_teams     [false]  Microsoft Teams (Azure Bot, /api/messages, approval cards)
```

Existing services:

```bash
copier update --data enable_teams=true      # or enable_web_chat=true
```

The update changes `app.py`, `pyproject.toml`, the Bicep, and adds `scripts/package_teams_app.py` and `tests/test_teams.py`. Your tools, instructions and evals are untouched.

The generated `app.py` looks like this:

```python
def channels() -> list:
    result: list = []
    result += [AgUiChannel(), WebChat(title="Order Status Agent")]
    result += teams_from_env()      # [] until AGENTKIT_TEAMS_APP_ID is set; the Bicep sets it
    return result

app = create_app(create_agent, channels=channels())
```

`teams_from_env()` means the same code runs on a laptop with no bot registration.

---

## Web chat

Open `https://<your-app>/chat`. You sign in with Entra through Easy Auth, the same sign-in that protects the API. Then you chat. Replies stream in, tool use shows as "Using lookup_order", and an approval shows as a panel:

- **Confirmation mode** (no approver role): **Approve** and **Reject** buttons, plus an optional comment. The run resumes in place.
- **Separation of duties** (`AGENTKIT_APPROVER_ROLE` set): "Waiting for an approver (Refunds.Approve)". The page checks every few seconds and says when an approver has decided (in Teams or through the approvals API).

To embed the chat in another page on the same origin:

```html
<script type="module" src="/chat/agentkit-chat.js"></script>
<agentkit-chat endpoint="/v1/agui" title="Orders"></agentkit-chat>
```

Styling uses CSS variables (`--akc-bg`, `--akc-fg`, `--akc-user`, `--akc-agent`, `--akc-border`), and dark mode is automatic.

### Easy Auth for browsers

The template sets Easy Auth to **redirect browsers to sign-in** when web chat is on (API clients without a token then get a redirect instead of a 401). On the Entra app registration behind `AGENTKIT_AUTH_CLIENT_ID`:

1. Add the redirect URI `https://<your-app>/.auth/login/aad/callback` (platform: Web).
2. Enable **ID tokens** (used for implicit and hybrid flows). No client secret is needed for sign-in.

### AG-UI for other clients

`POST /v1/agui` accepts a standard AG-UI `RunAgentInput` and streams standard events:

| Event | When |
|---|---|
| `RUN_STARTED` / `RUN_FINISHED` | Always. `outcome` is `{"type": "success"}` or `{"type": "interrupt", "interrupts": [...]}` |
| `TEXT_MESSAGE_START` / `_CONTENT` / `_END` | The agent's reply, streamed |
| `TOOL_CALL_START` / `_ARGS` / `_END` | The agent calling a tool (name and arguments). Tool *results* are not sent unless you pass `AgUiChannel(show_tool_results=True)` |
| `CUSTOM` `agentkit.blocked` | A guardrail refused the input; the refusal text is also sent as a message |
| `RUN_ERROR` | Something failed mid-stream |

An approval is one interrupt per pending tool call:

```json
{"id": "af-call-…", "reason": "tool_approval", "message": "Approve issue_refund?", "toolCallId": "call_1",
 "responseSchema": {"type": "object", "required": ["approved"], "properties": {"approved": {"type": "boolean"}, "comment": {"type": "string"}}},
 "metadata": {"tool": "issue_refund", "arguments": {"order_id": "A1002", "amount": 129},
              "awaiting": "requester", "approver_role": null, "status_url": "/v1/sessions/agui-t1/approvals"}}
```

To resume, send the same `threadId` with `resume: [{"interruptId": "<id>", "status": "resolved", "payload": {"approved": true, "comment": "ok"}}]`. `status: "cancelled"` rejects. Decide every pending interrupt in one request.

### Security, built in

- **The server keeps the conversation.** AG-UI clients send their whole history on every run. agentkit uses only the newest user message, so a browser can't rewrite what the agent said earlier (for example, "you promised me a $500 refund"). A test checks this.
- **Threads belong to users.** `threadId` maps to a session owned by the signed-in user. Another user sending the same id gets 403.
- **Nothing the agent writes is rendered as HTML.** The component uses `textContent` throughout, and a browser test sends `<img onerror=…>` to prove it.
- **Strict CSP** on the page (`script-src 'self'`, `connect-src 'self'`, `frame-ancestors 'none'`).
- **POSTs must be JSON** (415 otherwise), app-wide. A browser can't send JSON cross-site without a CORS preflight, so the sign-in cookie can't be abused by another site (CSRF).

---

## Microsoft Teams

### What happens on a message

1. Teams posts the activity to `/api/messages`. The endpoint validates the Bot Connector JWT (only on this route; Easy Auth skips it) and answers **202 at once**.
2. The turn runs in the background, with a typing indicator. The session is `teams-<hash(conversation, user)>`, owned by the user's Entra object id, in the same store as every other session.
3. The reply is posted proactively with the bot's managed identity.
4. If a tool needs approval, a card is posted (see below).

`reset` (or `new`) starts a fresh conversation. While an approval is pending, new messages get "I'm waiting for a decision on issue_refund…" instead of running.

### Approvals as cards

| Mode | Card goes to | Who can decide |
|---|---|---|
| Confirmation (no `AGENTKIT_APPROVER_ROLE`) | The requester's chat | Only the requester |
| Separation of duties (`AGENTKIT_APPROVER_ROLE` set) | The **approvals channel** (`AGENTKIT_TEAMS_APPROVALS_CHANNEL_ID`) as a new post, otherwise the requester's chat | Members of the approver directory, but never the requester |

When someone clicks:

- **Not allowed** (not an approver, or it's their own request): they alone see the reason, and the card stays.
- **Allowed**: the card is replaced at once with "Approved by Riley at 14:02 UTC" (no buttons left), the run resumes in the background, and the result is posted in the requester's chat as "Approved by Riley. The $129.00 refund on A1002 is done."
- **Already decided or expired** (a stale copy of the card): "This request was already decided or has expired." Nothing runs twice.

Every decision is in the session's audit log, with `channel: "teams"`, the approver's object id and display name, and the comment.

Tool arguments shown on cards come from the model, so every value is Markdown-escaped and truncated. An injected `[click here](https://…)` shows as text, not a link.

**Approver directory.** Easy Auth roles don't reach the bot endpoint, so in separation mode Teams needs its own list of who may approve. Startup fails without one:

| Setting | Meaning |
|---|---|
| `AGENTKIT_TEAMS_APPROVER_GROUP_ID` | Entra group; nested members count. Checked with Microsoft Graph `checkMemberGroups`, cached 5 minutes. Graph errors count as *not an approver*. |
| `AGENTKIT_TEAMS_APPROVERS` | Comma-separated object ids. Fine for a pilot. |

The group check needs the `GroupMember.Read.All` application permission for the service's managed identity, granted once by an admin:

```bash
MI=$(azd env get-value SERVICE_API_IDENTITY_PRINCIPAL_ID)
GRAPH=$(az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id -o tsv)
ROLE=$(az ad sp show --id 00000003-0000-0000-c000-000000000000 \
       --query "appRoles[?value=='GroupMember.Read.All'].id" -o tsv)
az rest --method POST --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$MI/appRoleAssignments" \
        --body "{\"principalId\":\"$MI\",\"resourceId\":\"$GRAPH\",\"appRoleId\":\"$ROLE\"}"
```

**The approvals channel.** Set `AGENTKIT_TEAMS_APPROVALS_CHANNEL_ID` to the channel's id (`19:…@thread.tacv2`; in Teams, use the channel's "Get link to channel" and URL-decode it). Add the app to that team, then **@mention the agent in the channel once** so it learns where to post. Until then, cards go to the requester's chat and a warning is logged.

### Deploy

```bash
azd env set AGENTKIT_APPROVER_ROLE Refunds.Approve                  # optional: separation of duties
azd env set AGENTKIT_TEAMS_APPROVER_GROUP_ID <group-object-id>      # needed with an approver role
azd env set AGENTKIT_TEAMS_APPROVALS_CHANNEL_ID "19:…@thread.tacv2"  # optional
azd up                                        # creates the Azure Bot + Teams channel
python scripts/package_teams_app.py           # build/teams-app.zip, from azd outputs
```

Upload `build/teams-app.zip` in Teams (**Apps > Manage your apps > Upload an app**) to try it, or through the Teams admin center for everyone.

`azd up` creates an **Azure Bot** whose identity is the service's user-assigned managed identity (`msaAppType: UserAssignedMSI`), so there's no client secret to store or rotate, and enables the Teams channel. The Container App gets `AGENTKIT_TEAMS_APP_ID` and `AGENTKIT_TEAMS_TENANT_ID`, and Easy Auth lets `/api/messages` through (the bot endpoint checks Bot Framework tokens itself). Bot SKU `F0` is free for Teams; set `botSku` to `S1` for an SLA.

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `AGENTKIT_TEAMS_APP_ID` | | Bot identity (managed identity client id); set by the Bicep |
| `AGENTKIT_TEAMS_TENANT_ID` | | Your tenant; set by the Bicep |
| `AGENTKIT_TEAMS_AUTH_TYPE` | `managed_identity` | `client_secret` for a dev tunnel with an app registration; `anonymous` for **local and test only** (inbound tokens not checked) |
| `AGENTKIT_TEAMS_CLIENT_SECRET` | | With `client_secret` only |
| `AGENTKIT_TEAMS_APPROVER_GROUP_ID` / `_APPROVERS` | | Approver directory (above) |
| `AGENTKIT_TEAMS_APPROVALS_CHANNEL_ID` | | Where approval cards go under separation of duties |
| `AGENTKIT_TEAMS_ALLOWED_SERVICE_HOSTS` | | Extra hosts the bot may call besides Microsoft's (tests, the local playground) |
| `AGENTKIT_TEAMS_ROUTE` | `/api/messages` | Bot endpoint path |
| `AGENTKIT_TEAMS_WELCOME` | | Message when the app is added to a chat |

Startup refuses: `anonymous` outside local/test; anything but `managed_identity` in prod; an approver role with no approver directory.

### Try it locally

The [Microsoft 365 Agents Playground](https://learn.microsoft.com/microsoftteams/platform/toolkit/debug-your-agents-playground) emulates Teams on your machine:

```bash
AGENTKIT_ENVIRONMENT=local AGENTKIT_TEAMS_AUTH_TYPE=anonymous AGENTKIT_TEAMS_ALLOWED_SERVICE_HOSTS=localhost \
  uvicorn my_agent.app:app --port 3978
teamsapptester start      # the playground; it targets http://localhost:3978/api/messages by default
```

---

## Testing channels

Teams, offline, through the real endpoint, SDK and cards:

```python
from agentkit.channels import StaticApprovers, TeamsChannel
from agentkit.channels.testing import TeamsTestClient, TeamsTestUser, teams_test_settings

SAM, RILEY = TeamsTestUser("sam"), TeamsTestUser("riley")

async def test_large_refund_needs_a_lead():
    teams_channel = TeamsChannel(teams_test_settings(approvals_channel_id=LEADS),
                                 approvers=StaticApprovers([RILEY.object_id]))
    app = create_app(lambda s: create_agent(s, client=scripted), settings=settings, channels=[teams_channel])
    async with TeamsTestClient(app) as teams:
        await teams.send("hi", user=RILEY, channel_id=LEADS)       # registers the leads channel
        await teams.send("Refund $129 on A1002", user=SAM)
        card = teams.last_card(LEADS)
        assert "role" in (await teams.click(card, "approve", user=SAM))["value"]   # not their own
        await teams.click(card, "approve", user=RILEY)
    assert teams.texts("a:sam")[-1].startswith("Approved by Riley.")
```

`TeamsTestClient.send` and `.click` wait for background turns, so assertions see the final state. `teams.texts(conversation_id)`, `.cards(...)` and `.last_card(...)` read what the agent posted. The full example is `examples/order-status-agent/tests/test_refund_approvals_teams.py`.

For AG-UI, use FastAPI's `TestClient` and parse the `data:` lines (see the generated `tests/test_app.py`). The kit's own browser tests (`packages/agentkit-channels/tests/test_webchat.py`) drive the real component in headless Chromium. They are skipped when Playwright isn't installed (`make browser` installs it; CI always runs them).

---

## Your own channel

Slack, a voice front end, an internal portal: implement `install()` and call the conversation service. You get every rule above for free.

```python
from agentkit.hosting import Caller, ConversationService, Decision

class SlackChannel:
    def install(self, app, service: ConversationService, settings):
        @app.post("/slack/events")
        async def events(request: Request):
            ...                                   # verify Slack's signature, map the user
            caller = Caller(user_id=slack_user_to_entra_oid(...), channel="slack")
            result = await service.run_turn(caller, text, session_id=f"slack-{thread}", create=True)
            ...                                   # post result.reply; render result.pending as buttons
```

`service.decide(caller, session_id, {approval_id: Decision(approved=True)})` resumes after a decision. `precheck_decision` validates one without running anything, for channels that must answer quickly. Errors are typed (`SessionNotFound`, `NotAllowed`, `ApprovalPending`, `NothingPending`, `InvalidDecisions`) with an HTTP-style `status`.

---

## Not included yet

- **Verified against a real Teams tenant.** Everything is tested offline with real Bot Framework activities through the M365 Agents SDK, a fake Bot Connector and a headless browser. Bot registration, the managed-identity token for replies, and card rendering in the Teams client still need a first live run.
- **Teams SSO for tools that act as the user** (on-behalf-of). Teams turns carry the user's identity but not their token, so `OnBehalfOfAuth` tools don't get one in Teams yet. Managed-identity tools work.
- **On-behalf-of tokens in web chat** need the Easy Auth token store and a client secret. They're off by default, so web-chat turns have no user token either.
- **Web chat history after a page reload.** The server keeps it, but the page starts empty (the agent still remembers).
- **Microsoft 365 Copilot** publishing.
