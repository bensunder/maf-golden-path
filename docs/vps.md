# Running it on a VPS (before Azure)

To try the kit on your own server first, `deploy/vps/` runs the sample agent with its web chat and console on any Linux host with Docker. You don't need Azure, a package feed or a pipeline.

```
Browser ─► Caddy (TLS) ─► oauth2-proxy (Entra sign-in) ─► agent + web chat + console ─► Redis (sessions)
                                                               └─► your model: Azure OpenAI or OpenAI, with an API key
```

About 20 minutes. You need Docker with Compose, a DNS name pointing at the server, and Caddy (or another reverse proxy) for TLS.

## 1. Get the code onto the server

Either download the release zip:

```bash
curl -L -o maf-golden-path.zip https://github.com/bensunder/maf-golden-path/archive/refs/tags/v0.9.1.zip
unzip maf-golden-path.zip && cd maf-golden-path-0.9.1/deploy/vps
```

or clone the tag:

```bash
git clone --branch v0.9.1 https://github.com/bensunder/maf-golden-path.git && cd maf-golden-path/deploy/vps
```

The zip is the source code. There is nothing to "install" from it directly: Docker builds the agent from it in step 4.

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
| Live charts, fleet view | Azure Monitor, managed identity | Not available (the Telemetry page says why). `APPLICATIONINSIGHTS_CONNECTION_STRING` still sends traces to Application Insights if you want them |
| Environment | `prod`, enforced policy | `dev`: the prod policy requires Azure, so the service doesn't claim it |

## Security notes

- **Identity comes only from oauth2-proxy.** The agent reads the user from `X-Forwarded-Email`, which oauth2-proxy sets after sign-in, replacing anything the browser sent. The Easy Auth headers are switched off, so sending `X-MS-CLIENT-PRINCIPAL-NAME` gets you nothing (tested).
- **Keep the agent private.** Never publish port 8000. If it were reachable directly, anyone could send `X-Forwarded-Email`. The compose file binds only oauth2-proxy, and only to 127.0.0.1.
- **Don't set** `AGENTKIT_APPROVER_ROLE` or `AGENTKIT_CONSOLE_ROLE` on a VPS. Without Easy Auth, role claims would come from a header nobody signs.
- `.env` holds secrets (model key, client secret, cookie secret). Keep it out of git and readable only by you (`chmod 600 .env`).

## Updating

```bash
# new release: download and unzip it (or git fetch && git checkout <tag>), copy your .env across, then
docker compose up -d --build
```

## Moving to Azure later

Nothing is lost. The agent code is the same: the VPS bundle builds the unchanged sample. When you're ready, follow [deploy.md](deploy.md), or run [live validation](live-validation.md) first to see the whole Azure setup work in a throwaway environment.
