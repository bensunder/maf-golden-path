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
git clone --branch v0.10.1 https://github.com/bensunder/maf-golden-path.git && cd maf-golden-path/deploy/vps
```

or download the release zip (`https://github.com/bensunder/maf-golden-path/archive/refs/tags/v0.10.1.zip`) and unzip it. The zip is the source code: Docker builds the agent from it in step 4.

## 2. An Entra app for sign-in

In the Entra admin center: **App registrations → New registration**.

- **Supported account types:** this organization only.
- **Redirect URI:** platform *Web*, `https://<your host>/oauth2/callback`.
- Then, under **Certificates & secrets → New client secret**, copy the value.

Note the **Application (client) ID** and the **Directory (tenant) ID**.

### Sign in with GitHub

Entra is the default. To let people sign in with a GitHub account instead, for example a partner or reviewer outside your tenant:

1. On GitHub: **Settings → Developer settings → OAuth Apps → New OAuth App**. Set the homepage URL to `https://<your host>` and the **Authorization callback URL** to `https://<your host>/oauth2/callback`. Copy the client ID, then **Generate a new client secret** and copy it.
2. In `.env`:

   ```bash
   SIGN_IN=github
   GITHUB_CLIENT_ID=<client id>
   GITHUB_CLIENT_SECRET=<client secret>
   GITHUB_USERS=yourlogin,theirlogin    # only these GitHub accounts; leave empty for any GitHub account
   ```

3. `python3 agentctl.py render`, then `docker compose up -d`.

Each person is identified by their GitHub account's primary, verified email, so chats, approvals and audit stay per person, as with Entra. Platform admins (`--admins`) are matched by that email, so list the address of your own GitHub account there. The console's Security page shows "GitHub sign-in". Leaving `GITHUB_USERS` empty means anyone with a GitHub account can sign in and chat, on your model key. Agents with their own host name need the callback URL of that host, so one GitHub OAuth app covers the agents served under `<your host>`.

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

## Create agents from the console

Turn on the platform service, and **Create agent** in the console builds and launches a real agent on this server. The developer never touches the server:

```bash
python3 agentctl.py platform --admins you@contoso.com,teammate@contoso.com
python3 agentctl.py fleet --internal        # optional: the fleet view at https://<AGENT_HOST>/fleet
docker compose up -d --build
```

Open `https://<AGENT_HOST>/console/agents`. It lists every agent on the server, with its health, version and who created it, and links to each one's console and chat.

**Create agent** (admins only) runs these steps, with a live build log:

1. generates a new agent from the kit's template (this checkout's version), built with Microsoft Agent Framework or [LangGraph](langgraph.md) (**Built with**), and, if you picked one, applies an agent template (below);
2. registers it;
3. builds it: the build runs the agent's offline evals, and a failing gate stops it there;
4. starts it and waits until it answers.

It then lives at `https://<AGENT_HOST>/agents/<name>/console` and `/chat`: no new DNS name, certificate, nginx site or Entra redirect URI. Its files are in `/opt/agents/<name>` (`--agents-dir` to change). Put that folder in its own git repository and edit its tools, instructions and eval cases; its CI runs the same evals on every change.

**How it's wired, and why it's safe:**

- The sign-in proxy forwards to the platform service, which routes `/agents/<name>/…` to that agent, `/fleet/…` to the fleet, and everything else to the sample.
- It tells each agent where it's mounted (`X-Agentkit-Prefix`, a setting agents read only when `AGENTKIT_FORWARDED_PREFIX_HEADER` is set), and it replaces any value a browser sends.
- Agents receive the signed-in user (`X-Forwarded-Email`) and nothing that could be replayed: the router drops the sign-in cookie and any `Authorization` header before a request reaches an agent.
- The router signs every request it forwards with that agent's own key (derived from `PLATFORM_SECRET_KEY`), and agents refuse requests that aren't signed for them. Something else on the Docker network can't pose as a signed-in person.
- The sample, the agents under `/agents/…` and the platform API share one web origin. Treat the agents on one server as one trust zone: an agent you don't trust belongs on its own stack or host.
- The platform service controls Docker on this host (it mounts the Docker socket), so:
  - it answers only the sign-in proxy's container, not other containers on the network;
  - only the emails in `--admins` can create or remove agents, only from the console's own page (the browser's `Origin` must be `https://<AGENT_HOST>`), and only admins see build logs;
  - names, titles, descriptions and teams are checked against strict patterns before they reach the template (no quotes, braces or backslashes);
  - every step runs as a fixed command with arguments, never through a shell;
  - one build runs at a time;
  - a failed build is rolled back (containers and registry entry), with the generated files kept in `/opt/agents/.failed/` for a look.
- Remove (admins, in the Agents list) stops the agent and moves its files to `/opt/agents/.removed/`.
- Agents with their own host name (`agentctl.py add` without `--internal`) keep working as before; the console lists them too.

`agentctl.py platform --off` puts the stack back to one sign-in proxy in front of the sample.

## Agents that call other agents

With the platform on, an agent can ask another for help, with the person's identity, guardrails, approvals and audit carried through the whole chain. In the console, set **Can call** on the Agents page. The **Agent network** page shows who may call whom, and every call. On the command line, `python3 agentctl.py peers <agent> <a,b>` does the same. MAF and LangGraph agents call each other both ways. The details, and how it's secured, are in [multi-agent.md](multi-agent.md).

The fleet view reads agents through the platform (`/read/<agent>/…`, the read-only overview, health and eval report only), so it works with signed agents too.

## Agent templates

**Start from → Browse templates** in Create agent picks a specialist to start from: a role and working procedure that becomes the new agent's instructions. The kit ships one library, [judicialmind/legal-agents](https://github.com/judicialmind/legal-agents) (30 legal specialists, MIT, pinned at `20587b4`), in `agent-templates/`.

The picker shows each template's scope, the rules added on top, the extra eval cases, and its full instructions to read before you use it. Choosing one fills in the name and description; the platform then:

- adds the template's role and procedure to `instructions/system.md`, below the kit's own rules (tool use, boundaries), followed by the library's rules, which take precedence over the role;
- fills the charter's scope from the template;
- appends the library's eval cases to `evals/cases.yaml`, so they're part of the build's quality gate;
- copies the template's license next to the instructions.

For the legal library the rules say the agent isn't a lawyer and gives no legal advice, never states a case, citation or statute as fact unless a tool returned it, marks drafts for professional review, asks which jurisdiction applies, and asks only for the facts it needs. Its two eval cases check the first two (`legal-no-invented-citation` is critical).

A template gives an agent expertise, not data: pair it with connectors (a contracts database, a case-law service) so it can look things up instead of saying it can't verify.

**More libraries:** `python3 agentctl.py templates add <name> <git-url> --ref <tag or commit>` clones one into `agent-templates/` (pin a commit you've reviewed: a template becomes an agent's instructions). `templates list` shows what's installed, `templates remove <name>` removes a cloned one. Agents made from a template keep their instructions when it's removed or updated. The format is in [agent-templates/README.md](../agent-templates/README.md).

## Connectors

With the platform service on, **Console → Connectors** is a catalog of the outside services your agents can use, like Claude's connector directory, but for your agents. It works with MCP servers that accept a service account or API token:

| Preset | MCP server | Credential |
|---|---|---|
| Linear | `https://mcp.linear.app/mcp` | API key |
| GitHub | `https://api.githubcopilot.com/mcp/` | fine-grained personal access token |
| Stripe | `https://mcp.stripe.com` | restricted key (`rk_…`) |
| Supabase | `https://mcp.supabase.com/mcp?project_ref=<ref>&read_only=true` | personal access token |
| Any MCP server | its streamable HTTP URL | none, bearer token, or a named header |

Services that only offer per-user OAuth (HubSpot, Google, Salesforce, Microsoft 365) aren't in this release.

**Add one (admins):** pick a preset, paste the key, and **Connect and list tools**. The platform connects, lists the server's tools, and you choose:

- which tools agents may use (read-only tools start on, tools that change things start off);
- which of those wait for a person to approve each call.

**Give it to agents:** in **Agents**, the **Connectors** button on a row (the sample included), or tick connectors in **Create agent**. The agent restarts with the connector's tools, named `<connector>_<tool>`. Its console lists them with a *Connector* tag and shows which need approval. The connector's page shows which agents use it and its recent calls (tool, agent, allowed or refused; never arguments or results). From there you can change the tools, list them again, or rotate the credential.

**How it's kept safe:**

- **The credential never reaches an agent.** It's stored in `connectors.json`, encrypted with `PLATFORM_SECRET_KEY` from `.env` (`agentctl.py platform` creates the key once; keep `.env` backed up, because without the key stored credentials can't be read and must be entered again). Agents talk to the platform's connector gateway (`http://platform:8001`, inside the Docker network only) with their own token, and the gateway adds the credential.
- **The gateway enforces the rules**, not the agent. It forwards only to the connector's saved URL, never a path or query the agent chose. It passes on only the MCP methods an agent needs, rejects ambiguous messages, and re-encodes what it forwards. It refuses tools that aren't allowed and hides them from the tool list. It refuses calls made under rules that have since changed, until the agent restarts with the new ones.
- **Only agents a connector is assigned to can use it**, and each agent's token is different.
- **Connector URLs must be `https` and resolve to public addresses**, checked on every call, so a connector can't be pointed at the server itself or the Docker network. The URL is shown only to admins.
- Approval uses the kit's normal human-approval flow, so write tools wait in the chat (and the Approvals page) for a person.

`connectors.json` belongs to this server and is ignored by git, like `agents.yaml`. From the command line: `python3 agentctl.py connect <agent|sample> linear,github` (or `none`), then `docker compose up -d`.

## More agents, and the fleet view

The same stack runs any number of agents, each at its own address with its own console, chat, sign-in cookie and Redis database, plus the [fleet view](fleet.md) across all of them.

**1. Generate an agent.** In any console: **Agents → Create agent**. Fill in the form and copy the command. Run it on the server (it needs `git`, and `copier`: `pipx install copier`):

```bash
mkdir -p /opt/agents && cd /opt/agents
copier copy --trust --vcs-ref v0.10.1 --data project_name='Legal Desk' ... gh:bensunder/maf-golden-path legal-desk
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
- **Isolation between agents:** agents share one Docker network and one Redis. Each has its own Redis user, limited to its own keys (`agentkit:<name>:`), so it can't read another agent's conversations. Behind the platform, each agent also accepts only requests the platform signed for it. Agents on one stack still share one host, so run agents that must not share a machine on separate hosts, or on Azure.

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
| Live charts | Azure Monitor, managed identity | Not available (the Telemetry page says why). `APPLICATIONINSIGHTS_CONNECTION_STRING` still sends traces to Application Insights, and `LANGSMITH_API_KEY` to [LangSmith](telemetry.md#langsmith), for every agent on the server |
| Fleet view | Azure discovery and Easy Auth tokens | `agentctl.py fleet`: the agents in this stack, read inside the Docker network. Its Traffic page needs Azure Monitor |
| Environment | `prod`, enforced policy | `dev`: the prod policy requires Azure, so the service doesn't claim it |

## Security notes

- **Identity comes only from oauth2-proxy.** The agent reads the user from `X-Forwarded-Email`, which oauth2-proxy sets after sign-in, replacing anything the browser sent. The Easy Auth headers are switched off, so sending `X-MS-CLIENT-PRINCIPAL-NAME` gets you nothing (tested).
- **Keep the agents private.** Never publish port 8000. If an agent were reachable directly, anyone could send `X-Forwarded-Email`. The compose files publish only the sign-in proxies, and only on 127.0.0.1. The same goes for anything else you run on the Docker network: agents trust `X-Forwarded-Email` from inside it (that's how the fleet reads them).
- **No roles on a VPS.** Without Easy Auth nothing signs role claims, so the stack switches role claims off (`AGENTKIT_PRINCIPAL_CLAIMS_HEADER` is empty) and any role check fails closed. Don't set `AGENTKIT_APPROVER_ROLE`, `AGENTKIT_CONSOLE_ROLE` or `AGENTKIT_FLEET_ROLE`: they would lock everyone out. `agentctl.py` refuses them.
- **Redis** has no default user. Each agent logs in as its own user, with a password `agentctl.py` generates (only hashes go into `redis/users.acl`). Redis is capped at 256 MB and drops the sessions closest to expiring first when full.
- **Containers** run with `no-new-privileges` and without `NET_RAW`.
- `.env` holds secrets (model key, client secret, cookie secret). Keep it out of git and readable only by you (`chmod 600 .env`).

## Updating

```bash
git fetch --tags && git checkout <new tag>      # or download and unzip it, and copy .env and agents.yaml across
python3 agentctl.py render                      # only if you run more agents
docker compose up -d --build
```

## Moving to Azure later

Nothing is lost. The agent code is the same: the VPS bundle builds the unchanged sample. When you're ready, follow [deploy.md](deploy.md), or run [live validation](live-validation.md) first to see the whole Azure setup work in a throwaway environment.
