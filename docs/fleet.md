# The fleet view

Each agent service has its own [console](console.md). The **fleet view** is one console for all of them: every agent's health, version, environment, security posture, quality gate, deployment and live traffic, side by side, with a link into each agent's own console.

```
https://<fleet>/console                 # Fleet · Security · Quality · Traffic
```

It's for the platform team, security and leadership: the people who need "what agents do we run, and are they all governed?" answered in one place.

## How it works

```
Browser ─► fleet service (Easy Auth) ──► each agent: GET /readyz, /v1/console/overview, /v1/console/evals
                 │                          (a token for the agent's own Entra app, from the fleet's managed identity)
                 └──────────────────────► Azure Monitor: runs, errors, latency, tokens per agent
```

- **Server-side, with its own identity.** The fleet service calls each agent with a token for that agent's Easy Auth app, from its managed identity. Browsers never call agents across sites, so no CORS or third-party cookies are involved.
- **Nothing copied.** Every page load asks the agents (cached for 30 seconds). An agent that doesn't answer shows as **Unreachable**, never as healthy. An agent without a console, or one that refuses the fleet's identity, is listed with the reason.
- **The same honesty rules as the console.** Security posture comes from each agent's running middleware stack. The quality gate is the report baked into the agent's deployed image.

## The registry: which agents it shows

Any mix of:

| Source | Setting | Use when |
|---|---|---|
| **Azure discovery** | `AGENTKIT_FLEET_DISCOVER=true` (default in the Bicep) | Agents generated from the template. Their Container Apps carry the `agentkit-service` and `agentkit-environment` tags; the fleet finds them with Azure Resource Graph (it gets Reader on the subscription) and shows the ones in its own environment |
| **A list** | `AGENTKIT_FLEET_AGENTS` (`url|api://<app id>|name` entries separated by `;`, or JSON) or `AGENTKIT_FLEET_REGISTRY` (a YAML file, see `fleet/registry.example.yaml`) | Agents in other subscriptions, older agents without the tags, or pinning the list |

Each entry has a `url`, an optional `name`, and the `audience`: the agent's Easy Auth app, `api://<client id>`. The same URL from both sources is shown once, with the registry's settings.

**Tokens go only where they belong.**
- For a discovered agent, the audience comes from the app's own Easy Auth configuration, never from a tag. Anyone who can tag a Container App can make the fleet *look* at it, but can't choose which token it gets.
- The fleet only mints tokens for `api://<GUID>` audiences. It never mints one for Azure management or Log Analytics.
- It sends tokens only over https (or to localhost), and never follows redirects.
- An agent that answers with something other than JSON is shown as an error and doesn't affect the others.

Use the semicolon form in azd environments: azd pastes values into `main.parameters.json` as text, so JSON's quotes would break it.

## Deploying it

Once per environment, next to the shared platform. It's an azd project in `fleet/`:

```bash
cd fleet
azd env new fleet-prod
python ../scripts/platform_env.py --resource-group rg-agentkit-prod | sh     # the platform values
azd env set AGENTKIT_ENVIRONMENT prod
azd env set AGENTKIT_FLEET_AUTH_CLIENT_ID <app id of the fleet's Entra app>   # required: the Bicep refuses to deploy a public fleet
azd up
```

The image is built from this repo's packages, so the fleet runs the kit version you checked out. The Bicep reuses the service template's modules (identity, Container App with Easy Auth, platform access). It grants the fleet identity:

- `Monitoring Reader` on the platform's Application Insights (traffic);
- `Reader` on the subscription (discovery; set `AGENTKIT_FLEET_DISCOVER=false` to skip it).

It gets nothing on Content Safety or the model gateway: the fleet runs no agent.

## Who can see what

- **Who opens the fleet view:** anyone who can sign in to its Entra app. To restrict it, add an app role (for example `Fleet.Read`), assign it, and set `AGENTKIT_FLEET_ROLE=Fleet.Read`.
- **What the fleet may read from agents:** by default, any agent accepts the fleet's app-only token, because agents accept any signed-in caller. If an agent sets `AGENTKIT_CONSOLE_ROLE`, assign that role to the fleet's managed identity on the agent's app registration. Otherwise the fleet shows that agent as "Console refused the fleet's identity".
- **What it never shows:** conversations, sessions and approvals. They stay private to their users, in each agent's own console. The fleet reads only the read-only overview and evals, which contain no endpoints, keys or messages.

## Pages

| Page | Shows |
|---|---|
| **Fleet** | Agents, ready, security controls all on, quality gate passed; one row per agent with health, environment, version, posture, gate, deploy time and commit, and a link to its console |
| **Security** | A matrix of every control × every agent (On / Partial / Off), with each agent's own explanation on hover |
| **Quality** | The gate result, runs passing, eval cases, and when it ran, per agent |
| **Traffic** | Requests, errors, error rate, average latency and model tokens per agent (1 hour, 24 hours or 7 days), from Azure Monitor |

## Running it locally

```bash
AGENTKIT_FLEET_AGENTS='[{"url": "http://127.0.0.1:8000", "name": "Orders"}]' uvicorn agentkit.channels.fleet:app --port 8020
```

Without `audience`, the fleet calls agents without a token, which is fine for a local agent with `AGENTKIT_REQUIRE_USER=false`. The tests in `packages/agentkit-channels/tests/test_fleet.py` run real agent servers behind a stand-in for Easy Auth.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `AGENTKIT_FLEET_AGENTS` | empty | Registered agents: `url|audience|name|public_url;…` or a JSON list. `public_url` is where people open an agent the fleet reaches at a private address |
| `AGENTKIT_FLEET_CALLER_HEADER` | none | `name: value` the fleet sends to agents registered without an audience, as its identity on a private network ([VPS](vps.md)). Never with a token; never `Authorization`, cookies or `x-ms-*` |
| `AGENTKIT_FLEET_REGISTRY` | none | Registered agents (YAML file) |
| `AGENTKIT_FLEET_DISCOVER` | `false` (`true` in the Bicep) | Find tagged agent services with Azure Resource Graph |
| `AGENTKIT_FLEET_SUBSCRIPTIONS` | the fleet's own subscription (Bicep) | Comma-separated subscriptions to search |
| `AGENTKIT_FLEET_ALL_ENVIRONMENTS` | off | Show discovered agents from every environment, not only the fleet's own |
| `AGENTKIT_FLEET_ROLE` | none | App role people need to open the fleet view |
| `AGENTKIT_CONSOLE_LOGS_RESOURCE` | set by the Bicep | Application Insights resource id for the Traffic page |
