# Live validation: prove it in your Azure subscription

Everything in CI runs offline: fake gateway, fake Bot Connector, fake search index, Bicep compiled and linted but never deployed. **Live validation** is the other half. A manual workflow deploys the shared platform and the sample agent into a throwaway environment in your subscription, runs real checks against real services, and then deletes everything.

```
Actions → live-validation → Run workflow        (~45–75 minutes, a few US dollars of Azure usage)
```

## What it proves

| Step | Checks |
|---|---|
| **What-if** | The platform and the sample's Bicep, previewed against your subscription before anything is created (saved as artifacts) |
| **Deploy** | Platform (API Management gateway, Azure OpenAI chat + embeddings, Content Safety, Container Apps, Cosmos DB, App Insights, dashboard, alerts), then the sample with `azd up`, with the kit pinned to the exact commit under test |
| **Knowledge** | The index is created and the sample's `knowledge/` folder is ingested with embeddings through the gateway |
| **Live checks** (`scripts/live_check.py`) | Easy Auth refuses anonymous calls. A chat answer comes through the gateway with managed identity, with token usage. The session continues. **Prompt Shields** refuses an injection. Streaming works. A large refund pauses for approval, the paused session answers 409, a rejection resumes the run. Knowledge fails closed for a caller that isn't a user |
| **Quality gate** | The sample's eval cases, live, 2× each, judged by the real model |
| **Judge calibration** | The judge scored against 12 human-graded answers: agreement, kappa, false passes |
| **Operations** | Every dashboard and alert query (`infra/platform/ops/queries.json`) runs against the real workspace; agent runs and gateway tokens must show up. The console's **live charts** must connect to Azure Monitor with the service's own identity and show the run's traffic |
| **Fleet view** | `fleet/` deploys with `azd up`; it must list the sample (registry and Azure discovery), see it ready, read its console with the fleet's managed identity, and query traffic |
| **Teardown** | `azd down --purge` (sample and fleet), the fleet's subscription-level Reader role removed, the platform resource group deleted, soft-deleted Cognitive Services and API Management instances purged so names and quota are released |

Results appear on the run page (step summaries) and as an artifact: `what-if-*.txt`, `live-check.json`, `gate.json`, `calibration.json`, `telemetry-check.json`, `fleet-check.json`.

## One-time setup (one command, about 5 minutes)

You need a subscription where you can create role assignments (a sandbox subscription is best), and permission to create app registrations in Entra ID. Then:

```bash
az login && gh auth login
scripts/setup_live_validation.sh --subscription <subscription-id> --repo <owner>/maf-golden-path --alert-email you@example.com
```

The script is safe to run again: it reuses what already exists, by name. It does the following, with no secrets anywhere:

1. **Workflow identity** `agentkit-live-validation`: an app registration with a federated credential for this repo's `live` environment, and Owner on the subscription (it creates resource groups and role assignments).
2. **Easy Auth app** `agentkit-live-api`, which protects the sample and the fleet view during the run (`api://<app id>`).
3. **Resource providers** the platform uses (Container Apps, API Management, Cognitive Services, Cosmos DB, Search, …).
4. **The GitHub environment `live`** with the variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `LIVE_AUTH_CLIENT_ID`, and `LIVE_ALERT_EMAIL` if you passed one.

Optional variables, set on the `live` environment yourself:

| Variable | Value |
|---|---|
| `LIVE_AUTH_RESOURCE` | default `api://$LIVE_AUTH_CLIENT_ID` |
| `LIVE_EVAL_USERS` | JSON mapping the sample's test users to real test accounts, e.g. `{"sam@contoso.example": "eval-support@yourtenant.com", "riley@contoso.example": "eval-lead@yourtenant.com"}` |

Add a required reviewer to the environment if you want a human to approve each run.

**Without `LIVE_EVAL_USERS`**, the gate skips the cases that run as a user (the knowledge permission cases); they stay covered offline. **With it**, grant the workflow identity `GroupMember.Read.All` (command in [channels.md](channels.md#approvals-as-cards), with the workflow app's object id). Put the eval accounts in the right groups, and set the group ids in the sample's `knowledge/acl.yaml`.

Then start a run:

```bash
gh workflow run live-validation.yml --repo <owner>/maf-golden-path
gh run watch --repo <owner>/maf-golden-path
```

## Running it

**Actions → live-validation → Run workflow.** Inputs:

- **location**: a region with `gpt-4.1-mini` (GlobalStandard) and `text-embedding-3-small` capacity (default `eastus2`).
- **keep**: skip teardown to poke around. Afterwards delete `rg-agentkit-live-<run id>` and the sample's `rg-aklive-<run id>`.
- **run-gate**: the live quality gate and judge calibration (on by default).

Runs don't overlap (a concurrency group). Teardown runs even when a step fails.

## Reading the results

- **A what-if you didn't expect** (a resource type blocked by policy, a missing provider registration): fix it before trusting anything else. The artifact has the full preview.
- **`live-check.json`**: hard checks must pass. The approval check is *soft*, because a real model sometimes asks a question before calling the tool.
- **`gate.json`**: live pass rates per case. The sample's `live-tracking-via-carrier-api` case is skipped because its carrier API is a placeholder host.
- **`calibration.json`**: under 80% agreement, or any false pass (the judge passing an answer a person failed), means don't let that judge gate deploys yet. Change `AGENTKIT_JUDGE_MODEL` or the rubrics.
- **`telemetry-check.json`**: a query that errors means the dashboard or an alert is broken. Fix `ops/queries.json`, then `python scripts/build_workbook.py`.

## Teams: the 30-minute manual checklist

The workflow can't click in Teams. After a run with **keep**:

1. The run's azd environment lived on the runner, so pass the values yourself. Get the bot id from the portal (Azure Bot → Configuration → Microsoft App ID) and the host from the Container App's URL: `cd examples/order-status-agent && python scripts/package_teams_app.py --bot-id <id> --host <app host>`.
2. Upload `build/teams-app.zip` in Teams (Apps → Manage your apps → Upload an app).
3. Chat with the agent: ask about an order, then ask for a $129 refund.
4. Confirm-mode card: click **Approve**. The card turns into "Approved by …" and the result arrives in the chat.
5. Optional: set `AGENTKIT_APPROVER_ROLE`, the approver group and the approvals channel, `azd up`, and repeat to see the card in the channel, and your own click refused.
6. Delete the resource groups.

## Not covered

- **Teams clicks** (above) and **browser sign-in to `/chat`** need a person.
- **Feed-mode installs**: the run pins the kit to git. See [deploy.md](deploy.md#package-feeds-and-dependency-confusion).
- **Private forks**: the pinned install fetches the kit from GitHub, so the repo must be public, or the Docker build needs a token.
