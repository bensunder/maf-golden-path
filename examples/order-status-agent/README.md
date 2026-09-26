# Order Status Agent

Answers order, shipping and small-refund questions for customer support staff.

Owned by **commerce**. Built on the agentkit golden path for Microsoft Agent Framework.

## What you own vs. what the platform owns

| You edit | The platform provides (agentkit) |
|---|---|
| `src/order_status_agent/tools.py`: tools | Gateway-bound model client, Entra ID auth |
| `src/order_status_agent/instructions/system.md`: behaviour | Prompt Shields / PII / tool-policy guardrails |
| `evals/cases.yaml`: expected behaviour | OpenTelemetry traces, metrics, request context |
| `agent.charter.md`: scope and accountability | Sessions, run limits, token budgets, HTTP API, CI |

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                          # unit tests + offline evals, no model needed
cp .env.example .env && az login
python -m order_status_agent    # http://localhost:8000
curl -s localhost:8000/v1/chat -H 'content-type: application/json' -d '{"message":"When is support open?"}'
```

Live evals against the gateway: `AGENTKIT_LIVE_EVALS=1 pytest tests/test_evals.py`.

Quality gate (what the deploy pipeline runs; scores answers with an LLM judge and compares with the baseline):

```bash
agentkit-gate --cases evals/cases.yaml --factory order_status_agent:create_agent --live --repeat 3 --baseline evals/baseline.json
# after an intentional behaviour change, refresh and commit the baseline:
agentkit-gate --cases evals/cases.yaml --factory order_status_agent:create_agent --live --repeat 5 --baseline evals/baseline.json --update-baseline
```

## API

- `POST /v1/chat` `{message, session_id?}` → `{session_id, status, reply, blocked, approvals, usage}`; `status` is `completed` or `approval_required`
- `POST /v1/chat/stream` → Server-Sent Events (`data: {"delta": ...}`, `event: approval_required` when paused, then `event: done`)
- `GET` / `POST /v1/sessions/{id}/approvals`: list pending approvals, or decide them (`{decisions: [{id, approved, comment?}]}`)
- `DELETE /v1/sessions/{id}`, `GET /healthz`, `GET /readyz`

The caller's identity comes from the `x-ms-client-principal-name` header set by platform auth (Container Apps / App Service Easy Auth, or APIM). Sessions can only be used by the user who created them.

## Where people use it

- **Web chat:** `https://<your-app>/chat` (Entra sign-in). The AG-UI endpoint behind it is `POST /v1/agui`, usable from CopilotKit or any AG-UI client. Approvals show as Approve/Reject buttons.
- **Console:** `https://<your-app>/console`: this agent's health, playground, approvals, eval results, sessions, security posture and deployments ([docs](https://github.com/bensunder/maf-golden-path/blob/v0.8.0/docs/console.md)).
- **Microsoft Teams:** `azd up` creates the Azure Bot. Then `python scripts/package_teams_app.py` builds `build/teams-app.zip` to upload in Teams. Approvals are Adaptive Cards (sent to the approvers channel if `AGENTKIT_TEAMS_APPROVALS_CHANNEL_ID` is set). Say `reset` to start over.

Setup and options: the kit's `docs/channels.md`.

## Company documents

`knowledge/` holds documents the agent can search, with who may read each one in `knowledge/acl.yaml`. Searches run as the signed-in user, and answers cite their sources. Every deploy syncs the folder to Azure AI Search; to preview: `agentkit-ingest --source knowledge --dry-run`. Details: the kit's `docs/knowledge.md`.

## Updating

`copier update` pulls template improvements. Bump the kit version in `pyproject.toml` to take package fixes.
