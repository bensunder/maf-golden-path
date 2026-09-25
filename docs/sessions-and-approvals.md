# Sessions and human approvals

Two things every production agent needs, and every team would otherwise build: conversations that survive restarts and scale-out, and a human in the loop for actions that are expensive to get wrong.

## Sessions: scale out without losing the conversation

A session is MAF's `AgentSession` (history and tool state) plus agentkit metadata: owner, pending approvals and an approval audit log. The host saves it after every turn and loads it at the start of the next, **inside a per-session lock**. Two messages for the same conversation never run at the same time, even on different replicas.

| `AGENTKIT_SESSION_STORE` | Use for | Auth | Notes |
|---|---|---|---|
| `memory` (default) | Tests, local dev | none | One process only |
| `cosmos` | **Azure services (what `azd up` configures)** | Managed identity, no keys | Serverless Cosmos DB in the platform; one container per service; data-plane role scoped to that container only |
| `redis` | Lowest latency, or where Cosmos isn't available | Connection string (`AGENTKIT_REDIS_URL`) | Token-checked distributed lock (`SET NX PX` + compare-and-delete) |

Expiry comes from `AGENTKIT_SESSION_TTL_SECONDS` (per-item TTL in Cosmos, `EX` in Redis). A replica that crashes while holding a lock doesn't wedge the conversation. The lock expires (120s by default), and stale Cosmos leases are cleared when found. A request that can't get the lock within 30s gets **409 "session is busy"**, and the client can retry.

With `azd up`, services get `AGENTKIT_SESSION_STORE=cosmos` and `maxReplicas: 5`. Nothing to write.

Your own store (DynamoDB, Postgres…) needs four methods: `get`, `put`, `delete` and `lock` (an async context manager). Pass it as `create_app(..., session_store=...)`. The contract tests in `packages/agentkit-hosting/tests/test_sessions.py` show exactly what it must guarantee.

## Human approvals

### 1. Mark the tool

```python
@tool(approval_mode="always_require")
def issue_refund(order_id: OrderId, amount: float, reason: str) -> str:
    ...
```

### 2. Auto-approve the low-risk calls (optional)

```python
from agentkit.hosting import approve_if

APPROVAL_RULES = [approve_if("issue_refund", lambda a: float(a["amount"]) <= 50)]
```

`create_agent` in the template already passes `APPROVAL_RULES` to `build_agent`. A rule that raises (a missing argument, say) **never** approves.

### 3. What callers see

A call that needs a human pauses the run. Nothing executes until someone decides:

```json
POST /v1/chat  {"message": "Refund the full $129 on A1002"}
→ {
    "session_id": "…",
    "status": "approval_required",
    "reply": "",
    "approvals": [{"id": "af-call-…", "tool": "issue_refund",
                   "arguments": {"order_id": "A1002", "amount": 129, "reason": "changed mind"},
                   "requested_at": 1790311388.9}]
  }
```

While an approval is pending, new messages to that session get **409** with the pending list, so the conversation can't drift past an undecided action.

```json
POST /v1/sessions/{session_id}/approvals
{"decisions": [{"id": "af-call-…", "approved": true, "comment": "customer called support"}]}
→ {"status": "completed", "reply": "I've refunded $129.00 on order A1002.", …}
```

- A **rejection** never runs the tool. The model is told the call was rejected and responds accordingly.
- You must decide **every** pending approval in one request. Unknown ids get 400 and missing ones get 409. Ids are checked against the session's pending list *before* anything reaches MAF.
- The response can be `approval_required` again: with auto-approval rules configured, MAF surfaces multiple pending calls one at a time.
- `GET /v1/sessions/{session_id}/approvals` lists what's pending, for an approver's inbox.
- Streaming (`/v1/chat/stream`) sends an `event: approval_required` before `event: done`.

### 4. Who may approve

| `AGENTKIT_APPROVER_ROLE` | Mode | Who can decide |
|---|---|---|
| empty (default) | **Confirmation** | The user who made the request ("are you sure?") |
| e.g. `Refunds.Approve` | **Separation of duties** | Anyone holding that Entra **app role**, *except* the requester (`AGENTKIT_APPROVAL_SEPARATION=false` allows self-approval) |

Roles come from Easy Auth's `X-MS-CLIENT-PRINCIPAL` header, so only signed-in, validated callers count. Define the role on the service's app registration and assign it to people or groups:

```bash
az ad app update --id "$APP_ID" --app-roles '[{"allowedMemberTypes":["User"],"description":"Approve refunds",
  "displayName":"Refunds approver","isEnabled":true,"value":"Refunds.Approve","id":"'$(uuidgen)'"}]'
# then Enterprise applications → <app> → Users and groups → add assignment
azd env set AGENTKIT_APPROVER_ROLE Refunds.Approve && azd up
```

When an approver resumes the run, the conversation stays attributed to its owner. Tools that use `OnBehalfOfAuth` run with the **approver's** delegated token, and spans carry `agentkit.approved_by`.

### 5. Audit

Every decision is appended to the session's `approval_log`: tool, arguments, approved or rejected, `decided_by`, `requested_by`, comment and timestamp. It's also counted in the `agentkit.approvals.requested` and `agentkit.approvals.decided` metrics (by tool and decision). For retention beyond the session TTL, ship the `agentkit.hosting.app` "approval … approved/rejected" log lines to your SIEM.

### 6. Policy still applies after approval

Approval answers "may this call happen?". `TOOL_POLICY` validators still run when the tool executes, so an approver can't authorise something the business rule forbids. In the sample, no one can refund more than the order total, approved or not.

## Testing

Eval cases pin the behaviour (see [testing-and-evals.md](testing-and-evals.md)):

```yaml
  - id: large-refund-approved
    input: Refund the full $129 on A1002.
    approve: true                          # decide every approval (true/false); omit to stop at the pause
    script:
      - tool: issue_refund
        args: {order_id: A1002, amount: 129, reason: changed mind}
      - reply: "Approved: I've refunded $129.00."
    expect:
      approval_required: [issue_refund]    # must pause…
      tools: [issue_refund]                # …and run after approval
```

`approval_required: []` asserts that *no* pause happened (the under-the-limit case).

In unit tests, a paused run has `result.user_input_requests`. Resume it with the same session:

```python
session = agent.create_session()
paused = await agent.run("refund $129 on A1002", session=session)
decision = Message(role="user", contents=[paused.user_input_requests[0].to_function_approval_response(approved=True)])
done = await agent.run(decision, session=session)
```

Outside the HTTP host (a queue worker, say), `build_agent` adds a throwaway session when you don't pass one, because MAF's approval middleware requires one. Pass your own session if you want to resume a paused run later.
