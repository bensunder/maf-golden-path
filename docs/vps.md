# Running it on a VPS (before Azure)

To try the kit on your own server first, `deploy/vps/` runs the sample agent with its web chat and console on any Linux host with Docker. Then [add your own agents and the fleet view](#more-agents-and-the-fleet-view) to the same stack. You don't need Azure, a package feed or a pipeline.

```
Browser ─► Caddy (TLS) ─► oauth2-proxy (Entra sign-in) ─► agent + web chat + console ─► Redis (sessions)
                                                               └─► your model: Azure OpenAI or OpenAI, with an API key
```

About 20 minutes. You need Docker with Compose, a DNS name pointing at the server, and Caddy (or another reverse proxy) for TLS.

## 1. Get the code onto the server

Clone the release tag (recommended: moving to a newer tag later updates the kit and every agent at once):

```bash
git clone --branch v0.9.2 https://github.com/bensunder/maf-golden-path.git && cd maf-golden-path/deploy/vps
```

or download the release zip (`https://github.com/bensunder/maf-golden-path/archive/refs/tags/v0.9.2.zip`) and unzip it. The zip is the source code: Docker builds the agent from it in step 4.

## 2. An Entra app for sign-in

In the Entra admin center: **App registrations → New registration**.

- **Supported account types:** this organization only.
- **Redirect URI:** platform *Web*, `https://<your host>/oauth2/callback`.
- Then, under **Certificates & secrets → New client secret**, copy the value.

Note the **Application (client) ID** and the **Directory (tenant) ID**.

## 3. Fill in `.env`

```bash
cp .env.example .env
openssl rand -base64 32 | tr -- '+/' '-_'     # paste as COOKIE_SECRET
nano .env
```

- **The model.** Use Azure OpenAI with a key (`MODEL_STYLE=azure`, the resource URL, the deployment name; `MODEL_API_VERSION` defaults to one that GPT-5 deployments accept). The model must support tool calling: in Ollama, for example, `phi4` doesn't and `phi4-mini` does. Or use OpenAI or any OpenAI-compatible proxy such as LiteLLM (`MODEL_STYLE=openai`, the base URL ending in `/v1`).
- **Sign-in.** The tenant id, client id and secret from step 2, plus `AGENT_HOST`. Set `ALLOWED_EMAIL_DOMAINS` to your domain to limit who can sign in beyond "anyone in the tenant".

## 4. Start it

```bash
docker compose up -d --build        # first build: a few minutes
docker compose logs -f agent        # "Application startup complete"
```

The build runs the sample's offline evals, so the console's Evaluations page shows the result for exactly this build.

## 5. Put Caddy or nginx in front

- **Caddy:** add the block from `Caddyfile.example` to your Caddyfile (it proxies to `127.0.0.1:4180`), then `caddy reload`.
- **nginx:** copy `nginx.conf.example` to `/etc/nginx/sites-available/`, set your host name, enable it, then `certbot --nginx -d <your host> --redirect`. Keep `proxy_buffering off` (chat replies stream) and the larger buffers (sign-in cookies).

No DNS name yet? `agent.<your-ip-with-dashes>.sslip.io` (for example `agent.203-0-113-7.sslip.io`) resolves to your server and gets a certificate like any other name.

Open:

- `https://<your host>/chat`: the web chat;
- `https://<your host>/console`: the console.

## More agents, and the fleet view

The same stack runs any number of agents, each at its own address with its own console, chat, sign-in cookie and Redis database, plus the [fleet view](fleet.md) across all of them.

**1. Generate an agent.** In any console: **Agents → Create agent**. Fill in the form and copy the command. Run it on the server (it needs `git`, and `copier`: `pipx install copier`):

```bash
mkdir -p /opt/agents && cd /opt/agents
copier copy --trust --vcs-ref v0.9.2 --data project_name='Legal Desk' ... gh:bensunder/maf-golden-path legal-desk
```

Edit its `instructions/system.md`, `tools.py` and `evals/cases.yaml`. Put it in its own git repository: the generated CI runs its evals on every change.

**2. Add it to the stack:**

```bash
cd <this folder>/deploy/vps
python3 agentctl.py add legal /opt/agents/legal-desk      # → https://legal.<your domain>
python3 agentctl.py fleet                                # the fleet view → https://fleet.<your domain>
```

`agentctl.py` writes `docker-compose.agents.yml`, one nginx site per host name in `nginx/`, and `COMPOSE_FILE` in `.env` (so plain `docker compose` includes the agents). It prints the rest, which it leaves to you because it changes things outside this folder:

1. copy the nginx site into `/etc/nginx/sites-available`, enable it and reload nginx (copy, don't link: certbot adds HTTPS to that copy, and `agentctl.py` never touches it again);
2. `certbot --nginx -d <host> --redirect`;
3. add the new host's redirect URI to the Entra app (`az ad app update ... --web-redirect-uris ...`, printed with every URI);
4. `docker compose up -d --build`.

**How it fits together:**

- **One image recipe for every agent.** `Dockerfile` builds whichever agent folder it's given, with the kit's packages from this checkout. So every agent runs the same kit version, and `git pull` upgrades them all. The build runs the agent's offline evals as its quality gate, and the console shows that report.
- **Shared model settings.** Agents use the model settings in `.env`; override one agent with `--env AGENTKIT_MODEL=gpt-5-mini`.
- **The fleet reads agents inside the Docker network.** It identifies itself with `X-Forwarded-Email: fleet@agentkit.local` (`AGENTKIT_FLEET_CALLER_HEADER`), and it links people to each agent's public address. It reads only the read-only overview and eval report: never conversations or approvals.
- **Other commands:** `python3 agentctl.py list`, `remove <name>`, `fleet --off`, and `render` (rewrite the files after editing `agents.yaml` by hand; it checks every entry first). `remove` prints the nginx clean-up, because a site left enabled for a removed agent stops nginx from reloading.
- **Caddy instead of nginx:** add one block per host, `legal.<your domain> { reverse_proxy 127.0.0.1:<port> }`, with the port from `python3 agentctl.py list`.
- **Isolation between agents:** agents share one Docker network and one Redis (a database each). Inside the network they trust `X-Forwarded-Email`, so treat the agents on one stack as one trust zone. Run agents that must be isolated from each other on separate stacks or hosts, or on Azure.

`agents.yaml`, `docker-compose.agents.yml` and `nginx/` belong to this server and are ignored by git.

## What's different from Azure

The console shows these honestly as *Partial*, so nothing here is hidden.

| | On Azure | On this VPS |
|---|---|---|
| Sign-in | Easy Auth in front of every request | oauth2-proxy with the same Entra tenant. The agent is reachable only through it (it isn't published on the host) |
| Prompt injection | Prompt Shields, required in prod | Heuristic detector. Set `GUARDRAIL_MODE=prompt_shields` with an Azure AI Content Safety endpoint and key to use Prompt Shields |
| Model access | Managed identity through the AI gateway (quotas, chargeback) | Directly to the model with an API key |
| Approvals | Confirmation, or an approver role with separation of duties | Confirmation only: the person who asked confirms. Approver roles need Easy Auth's signed role claims |
| Sessions | Cosmos DB | Redis in a Docker volume |
| Knowledge | Azure AI Search, trimmed per user | Off: the search tool reports that it can't search and the agent says so |
| Live charts | Azure Monitor, managed identity | Not available (the Telemetry page says why). `APPLICATIONINSIGHTS_CONNECTION_STRING` still sends traces to Application Insights if you want them |
| Fleet view | Azure discovery and Easy Auth tokens | `agentctl.py fleet`: the agents in this stack, read inside the Docker network. Its Traffic page needs Azure Monitor |
| Environment | `prod`, enforced policy | `dev`: the prod policy requires Azure, so the service doesn't claim it |

## Security notes

- **Identity comes only from oauth2-proxy.** The agent reads the user from `X-Forwarded-Email`, which oauth2-proxy sets after sign-in, replacing anything the browser sent. The Easy Auth headers are switched off, so sending `X-MS-CLIENT-PRINCIPAL-NAME` gets you nothing (tested).
- **Keep the agents private.** Never publish port 8000. If an agent were reachable directly, anyone could send `X-Forwarded-Email`. The compose files publish only the sign-in proxies, and only on 127.0.0.1. The same goes for anything else you run on the Docker network: agents trust `X-Forwarded-Email` from inside it (that's how the fleet reads them).
- **No roles on a VPS.** Without Easy Auth nothing signs role claims, so the stack switches role claims off (`AGENTKIT_PRINCIPAL_CLAIMS_HEADER` is empty) and any role check fails closed. Don't set `AGENTKIT_APPROVER_ROLE`, `AGENTKIT_CONSOLE_ROLE` or `AGENTKIT_FLEET_ROLE`: they would lock everyone out. `agentctl.py` refuses them.
- `.env` holds secrets (model key, client secret, cookie secret). Keep it out of git and readable only by you (`chmod 600 .env`).

## Updating

```bash
git fetch --tags && git checkout <new tag>      # or download and unzip it, and copy .env and agents.yaml across
python3 agentctl.py render                      # only if you run more agents
docker compose up -d --build
```

## Moving to Azure later

Nothing is lost. The agent code is the same: the VPS bundle builds the unchanged sample. When you're ready, follow [deploy.md](deploy.md), or run [live validation](live-validation.md) first to see the whole Azure setup work in a throwaway environment.
