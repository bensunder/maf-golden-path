"""The fleet view: every agent service in one console.

A small service (``create_fleet_app``) that knows where the agents are (a **registry**), asks each one's
read-only console API for its overview with its own managed identity, and serves the console in fleet
mode: health, version, environment, security posture, quality gate and deploy metadata for every agent,
plus live traffic per agent from Azure Monitor.

    uvicorn agentkit.channels.fleet:app            # configured from the environment

The registry comes from any mix of:

* ``AGENTKIT_FLEET_REGISTRY``: a YAML file listing agents (``url``, optional ``name``, ``audience``);
* ``AGENTKIT_FLEET_AGENTS``: the same list as JSON, or ``url|audience|name|public_url`` entries separated by
  ``;`` (safe to pass through azd parameter files). ``public_url`` is where people open the agent, when the
  fleet reaches it at another address (a private network);
* ``AGENTKIT_FLEET_DISCOVER=true``: Azure Resource Graph, for Container Apps tagged ``agentkit-service`` in
  this environment. The token audience comes from each app's own Easy Auth configuration, never from a tag.

The fleet only ever mints tokens for ``api://<app id>`` audiences (an agent's Easy Auth app), and never
sends one over plain http except to localhost.

On a private network without Easy Auth (``deploy/vps``), ``AGENTKIT_FLEET_CALLER_HEADER`` (``name: value``,
for example ``x-forwarded-email: fleet@agentkit.local``) is sent as the identity the fleet reads agents with,
only to agents registered without an audience at a private address: plain http to a Docker service name
(no dots), localhost or a private IP. Only use it where the agents can't be reached from outside.

Nothing here is a copy of the agents' data: each request asks the agents (cached for 30 seconds), and an
agent that doesn't answer is shown as unreachable, never as healthy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from pydantic_settings import BaseSettings, SettingsConfigDict

from agentkit.hosting import AgentKitSettings
from agentkit.hosting.app import _JsonOnly
from agentkit.hosting.approvals import roles_from_principal

from .traffic import RANGES, AzureLogsClient, LogsClient, LogsQueryError, TrafficQueries, TtlCache

__all__ = ["FleetAgent", "FleetRegistry", "FleetService", "FleetSettings", "create_fleet_app", "discover_agents", "load_registry"]

logger = logging.getLogger(__name__)
_API_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
TokenFor = Callable[[str], Awaitable[str | None]]


_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def normalize_audience(value: str | None) -> str | None:
    """``api://<guid>`` (or a bare guid). Anything else could make the fleet mint tokens for other resources
    (Azure management, Log Analytics) and send them to whoever runs the URL, so it's refused."""
    if not value:
        return None
    value = value.strip().removesuffix("/.default").rstrip("/")
    guid = value[len("api://"):] if value.startswith("api://") else value
    if not _GUID.match(guid):
        raise ValueError(f"fleet: audience must be api://<app id> (a GUID), got {value!r}")
    return f"api://{guid.lower()}"


@dataclass(frozen=True)
class FleetAgent:
    url: str  # https://ca-orders.azurecontainerapps.io
    name: str | None = None  # display name if the agent doesn't answer
    audience: str | None = None  # Easy Auth app (api://<client id>); None = call without a token (local)
    source: str = "registry"  # registry | discovered | both
    public_url: str | None = None  # where people open it, if the fleet reaches it at a private address

    def __post_init__(self) -> None:
        for field in ("url", "public_url"):
            value = getattr(self, field)
            if value is None and field == "public_url":
                continue
            value = str(value).rstrip("/")
            parsed = urlparse(value)
            if parsed.scheme not in ("https", "http") or not parsed.netloc:
                raise ValueError(f"fleet registry: not an http(s) URL: {getattr(self, field)!r}")
            object.__setattr__(self, field, value)
        object.__setattr__(self, "audience", normalize_audience(self.audience))

    @property
    def private_address(self) -> bool:
        """A Docker service name, localhost or a private IP, over plain http: where a caller identity may go."""
        import ipaddress

        parsed = urlparse(self.url)
        host = parsed.hostname or ""
        if parsed.scheme != "http":
            return False
        if "." not in host and ":" not in host:
            return True  # a service name on the Docker network
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return host == "localhost"
        return ip.is_private or ip.is_loopback

    @property
    def console_url(self) -> str:
        return (self.public_url or self.url) + "/console"

    @property
    def token_allowed(self) -> bool:
        """Tokens travel only over https (or to this machine, for local development)."""
        parsed = urlparse(self.url)
        return parsed.scheme == "https" or parsed.hostname in ("localhost", "127.0.0.1", "::1")

    @property
    def key(self) -> str:
        return self.url.rstrip("/").lower()


def _agent(raw: dict[str, Any], source: str) -> FleetAgent:
    return FleetAgent(url=str(raw.get("url") or ""), name=raw.get("name") or None,
                      audience=raw.get("audience") or None, source=source, public_url=raw.get("public_url") or None)


def _parse_inline(value: str) -> list[dict[str, Any]]:
    value = value.strip()
    if not value:
        return []
    if value.startswith("["):
        return json.loads(value)
    entries = []
    for item in value.split(";"):
        if item.strip():
            url, audience, name, public_url = (item.split("|") + ["", "", ""])[:4]
            entries.append({"url": url.strip(), "audience": audience.strip() or None, "name": name.strip() or None,
                            "public_url": public_url.strip() or None})
    return entries


def load_registry(path: str | Path | None = None, inline: str | None = None) -> list[FleetAgent]:
    agents: list[FleetAgent] = []
    if path:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        agents += [_agent(a, "registry") for a in data.get("agents", [])]
    if inline:
        agents += [_agent(a, "registry") for a in _parse_inline(inline)]
    return agents


class FleetRegistry:
    """Configured agents plus (optionally) discovered ones, de-duplicated by URL."""

    def __init__(self, agents: list[FleetAgent] = (), discover: Callable[[], Awaitable[list[FleetAgent]]] | None = None,
                 refresh_seconds: float = 300.0):
        self.static = list(agents)
        self.discover = discover
        self.refresh_seconds = refresh_seconds
        self._found: list[FleetAgent] = []
        self._found_at = 0.0
        self.discovery_error: str | None = None

    async def agents(self) -> list[FleetAgent]:
        # an empty result is often a role assignment that hasn't propagated yet: look again sooner
        wait = self.refresh_seconds if self._found else min(self.refresh_seconds, 30.0)
        if self.discover and time.monotonic() - self._found_at > wait:
            try:
                self._found = await self.discover()
                self.discovery_error = None
            except Exception as exc:  # keep the last good list; say discovery failed
                logger.warning("fleet discovery failed: %s", exc)
                self.discovery_error = "Azure discovery failed; showing registered agents and the last discovered list."
            self._found_at = time.monotonic()
        seen: dict[str, FleetAgent] = {a.key: a for a in self.static}
        for found in self._found:
            mine = seen.get(found.key)
            if mine is None:
                seen[found.key] = found
            elif mine.source == "registry":  # listed and discovered: keep the registry's settings
                seen[found.key] = FleetAgent(url=mine.url, name=mine.name or found.name,
                                             audience=mine.audience or found.audience, source="both",
                                             public_url=mine.public_url)
        return list(seen.values())


_DISCOVERY_QUERY = """
resources
| where type =~ "microsoft.app/containerapps" and isnotempty(tags["agentkit-service"])
| extend environment = tostring(tags["agentkit-environment"])
| project id, name, fqdn = tostring(properties.configuration.ingress.fqdn), service = tostring(tags["agentkit-service"]),
          environment
| where isnotempty(fqdn)
""".strip()

_ARM = "https://management.azure.com"


async def discover_agents(token: Callable[[], Awaitable[str]], *, subscriptions: list[str] | None = None,
                          environment: str | None = None, client: httpx.AsyncClient | None = None) -> list[FleetAgent]:
    """Agent services in Azure: Container Apps the template tagged ``agentkit-service``. Needs Reader.

    Tags only *find* candidates. The audience the fleet mints a token for is read from each app's own Easy
    Auth configuration (``authConfigs/current``), so tagging an app can't make the fleet hand out tokens."""
    http = client or httpx.AsyncClient(timeout=30)
    try:
        headers = {"Authorization": f"Bearer {await token()}"}
        rows: list[dict[str, Any]] = []
        body: dict[str, Any] = {"query": _DISCOVERY_QUERY, "options": {"$top": 1000}}
        if subscriptions:
            body["subscriptions"] = subscriptions
        for _ in range(20):  # pages of 1000
            response = await http.post(f"{_ARM}/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01",
                                       json=body, headers=headers)
            response.raise_for_status()
            page = response.json()
            rows += page.get("data") or []
            skip = page.get("$skipToken")
            if not skip:
                break
            body["options"] = {"$top": 1000, "$skipToken": skip}
        agents = []
        for r in rows:
            if environment and r.get("environment") and r["environment"] != environment:
                continue  # another environment's agent (dev in a prod fleet)
            audience = await _easy_auth_audience(http, headers, r["id"])
            try:
                agents.append(FleetAgent(url=f"https://{r['fqdn']}", name=r.get("service") or r.get("name"),
                                         audience=audience, source="discovered"))
            except ValueError as exc:
                logger.warning("fleet discovery: skipping %s: %s", r.get("name"), exc)
        return agents
    finally:
        if client is None:
            await http.aclose()


async def _easy_auth_audience(http: httpx.AsyncClient, headers: dict[str, str], app_id: str) -> str | None:
    response = await http.get(f"{_ARM}{app_id}/authConfigs/current?api-version=2024-03-01", headers=headers)
    if response.status_code != 200:
        return None  # no Easy Auth: the fleet calls without a token and the agent decides
    config = response.json().get("properties") or {}
    aad = (config.get("identityProviders") or {}).get("azureActiveDirectory") or {}
    client_id = (aad.get("registration") or {}).get("clientId")
    if not (config.get("platform") or {}).get("enabled", True) or not client_id or not _GUID.match(str(client_id)):
        return None
    return f"api://{client_id}"


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


# ------------------------------------------------------------------------------------ service
class FleetService:
    def __init__(self, registry: FleetRegistry, token_for: TokenFor, *, logs: LogsClient | None = None,
                 client: httpx.AsyncClient | None = None, timeout: float = 10.0, cache_seconds: float = 30.0,
                 caller_header: tuple[str, str] | None = None):
        self.registry = registry
        self.caller_header = caller_header  # identity for agents without an audience (private networks only)
        self.token_for = token_for
        self.logs = logs
        self.client = client
        self.timeout = timeout
        self._cache = TtlCache(cache_seconds)
        self._traffic_cache = TtlCache(60.0)
        self._limit = asyncio.Semaphore(10)

    async def _get(self, http: httpx.AsyncClient, agent: FleetAgent, path: str, token: str | None) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif self.caller_header and not agent.audience and agent.private_address:
            headers[self.caller_header[0]] = self.caller_header[1]
        return await http.get(agent.url + path, headers=headers, timeout=self.timeout)

    async def _probe(self, http: httpx.AsyncClient, agent: FleetAgent) -> dict[str, Any]:
        async with self._limit:
            try:
                return await self._probe_one(http, agent)
            except Exception:  # one broken agent never takes the fleet page down
                logger.exception("fleet: probing %s failed", agent.url)
                return {"url": agent.url, "console_url": agent.console_url, "source": agent.source,
                        "name": agent.name, "status": "unreachable", "console": "error",
                        "detail": "The fleet couldn't read this agent's answer.", "overview": None, "gate": None,
                        "checked_at": time.time()}

    async def _probe_one(self, http: httpx.AsyncClient, agent: FleetAgent) -> dict[str, Any]:
        entry: dict[str, Any] = {"url": agent.url, "console_url": agent.console_url, "source": agent.source,
                                 "name": agent.name, "status": "unreachable", "detail": None,
                                 "overview": None, "gate": None, "checked_at": time.time()}
        try:
            ready = await self._get(http, agent, "/readyz", None)
            entry["status"] = "ready" if ready.status_code == 200 else "not_ready"
            if ready.status_code != 200:
                entry["detail"] = f"/readyz answered HTTP {ready.status_code}"
        except httpx.HTTPError as exc:
            entry["detail"] = f"can't reach the service ({type(exc).__name__})"
            return entry
        if agent.audience and not agent.token_allowed:
            entry["console"] = "error"
            entry["detail"] = "The fleet won't send a token over plain http; register the agent's https URL."
            return entry
        try:
            token = await self.token_for(agent.audience) if agent.audience else None
        except Exception as exc:
            logger.warning("fleet: no token for %s: %s", agent.audience, exc)
            entry["console"] = "no_token"
            entry["detail"] = "The fleet service couldn't get a token for this agent's app."
            return entry
        try:
            overview = await self._get(http, agent, "/v1/console/overview", token)
        except httpx.HTTPError:
            entry["console"] = "unreachable"
            return entry
        if overview.status_code == 404:
            entry["console"] = "not_installed"
            return entry
        if overview.status_code in (401, 403):
            entry["console"] = "denied"
            entry["detail"] = ("The agent refused the fleet's identity: give it the agent's console role "
                               "(AGENTKIT_CONSOLE_ROLE) if one is set.")
            return entry
        if overview.status_code != 200:
            entry["console"] = "error"
            entry["detail"] = f"console API answered HTTP {overview.status_code}"
            return entry
        body = _json_object(overview)
        if body is None:
            entry["console"] = "error"
            entry["detail"] = "The console API didn't answer with JSON."
            return entry
        entry["console"] = "ok"
        entry["overview"] = {k: body.get(k) for k in ("service", "agent", "channels", "knowledge", "security",
                                                       "sessions", "approvals", "telemetry")}
        if entry["overview"]["agent"]:
            entry["overview"]["agent"].pop("limits", None)
        entry["name"] = (body.get("service") or {}).get("title") or entry["name"]
        try:
            evals = await self._get(http, agent, "/v1/console/evals", token)
            data = _json_object(evals) if evals.status_code == 200 else None
            if data is not None:
                report = data.get("report") if isinstance(data.get("report"), dict) else None
                entry["gate"] = {"cases": len(data.get("cases") or []),
                                 "report": None if not report else {
                                     "passed": report.get("passed"), "live": report.get("live"),
                                     "started_at": report.get("started_at"),
                                     "passed_runs": sum(c.get("passed_runs") or 0 for c in report.get("cases") or []),
                                     "runs": sum(c.get("runs") or 0 for c in report.get("cases") or [])},
                                 "error": data.get("report_error")}
        except httpx.HTTPError:
            pass
        return entry

    async def agents(self) -> dict[str, Any]:
        cached = self._cache.get("agents")
        if cached is not None:
            return cached
        registered = await self.registry.agents()
        http = self.client or httpx.AsyncClient()
        try:
            entries = await asyncio.gather(*(self._probe(http, a) for a in registered))
        finally:
            if self.client is None:
                await http.aclose()
        body = {"agents": list(entries), "checked_at": time.time(), "discovery_error": self.registry.discovery_error,
                "registered": len(self.registry.static), "discovery": self.registry.discover is not None}
        self._cache.put("agents", body)
        return body

    async def traffic(self, range_: str) -> dict[str, Any]:
        if self.logs is None:
            return {"available": False, "range": range_,
                    "reason": "Set AGENTKIT_CONSOLE_LOGS_RESOURCE to the platform's Application Insights resource id."}
        cached = self._traffic_cache.get(range_)
        if cached is not None:
            return cached
        timespan = RANGES[range_][0]
        window = {"PT1H": "1h", "PT24H": "24h", "P7D": "7d"}[timespan]
        try:
            raw = await self.logs.query(TrafficQueries.fleet(window), timespan)
        except LogsQueryError as exc:
            return {"available": False, "range": range_, "error": str(exc)}
        agents = []
        for r in raw:
            count = float(r.get("DurationCount") or 0)
            runs = float(r.get("Runs") or 0)
            services = r.get("Services")
            if isinstance(services, str):
                try:
                    services = json.loads(services)
                except ValueError:
                    services = [services]
            agents.append({"agent": r.get("Agent") or "unknown", "services": [s for s in services or [] if s],
                           "runs": runs, "errors": float(r.get("Errors") or 0), "blocked": float(r.get("Blocked") or 0),
                           "avg_s": float(r.get("DurationSum") or 0) / count if count else None,
                           "tokens": float(r.get("Tokens") or 0),
                           "error_rate": float(r.get("Errors") or 0) / runs if runs else None})
        agents.sort(key=lambda a: (-a["runs"], -a["tokens"]))
        body = {"available": True, "range": range_, "queried_at": time.time(), "agents": agents}
        self._traffic_cache.put(range_, body)
        return body


# ------------------------------------------------------------------------------------ app
class FleetSettings(BaseSettings):
    """The fleet service's own settings (``AGENTKIT_*``). It runs no agent, so the agent prod policy
    (gateway, Prompt Shields) doesn't apply; it always requires a signed-in user."""

    model_config = SettingsConfigDict(env_prefix="AGENTKIT_", env_file=".env", extra="ignore")

    environment: str = "local"
    service_version: str = "0.0.0"
    auth_mode: str = "default"
    managed_identity_client_id: str | None = None
    require_user: bool = True
    user_header: str = "x-ms-client-principal-name"
    user_fallback_header: str = "x-ms-client-principal-id"
    principal_claims_header: str = "x-ms-client-principal"


def parse_caller_header(value: str | None) -> tuple[str, str] | None:
    """``name: value`` → (name, value). Never an Authorization or cookie header: this is an identity for
    agents on a private network, not a credential."""
    if not value or not value.strip():
        return None
    name, sep, content = value.partition(":")
    name, content = name.strip().lower(), content.strip()
    if not sep or not re.fullmatch(r"[a-z0-9-]+", name) or not content:
        raise ValueError("AGENTKIT_FLEET_CALLER_HEADER must look like 'x-forwarded-email: fleet@agentkit.local'")
    if name not in ("x-forwarded-email", "x-forwarded-user") and not name.startswith("x-agentkit-"):
        raise ValueError(f"AGENTKIT_FLEET_CALLER_HEADER can't set {name}: use x-forwarded-email, "
                         "x-forwarded-user or an x-agentkit-* header")
    return name, content


def _token_for(settings: Any) -> TokenFor:
    from agentkit.hosting.clients import get_credential

    credential = get_credential(settings)

    async def token_for(audience: str | None) -> str | None:
        if not audience or credential is None:
            return None
        scope = audience if audience.endswith("/.default") else audience.rstrip("/") + "/.default"
        return (await credential.get_token(scope)).token

    return token_for


def create_fleet_app(
    registry: FleetRegistry | None = None,
    *,
    settings: FleetSettings | AgentKitSettings | None = None,
    token_for: TokenFor | None = None,
    logs: LogsClient | None = None,
    role: str | None = None,
    title: str = "Agent fleet",
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """The fleet service. Everything defaults from the environment (see the module docstring); tests pass
    a registry, ``token_for`` and a fake ``logs``."""
    from .console import _SECURITY_HEADERS, _bundle, _html_attr, _ASSET_TYPES

    settings = settings or FleetSettings()
    role = role if role is not None else (os.getenv("AGENTKIT_FLEET_ROLE") or None)
    if registry is None:
        agents = load_registry(os.getenv("AGENTKIT_FLEET_REGISTRY"), os.getenv("AGENTKIT_FLEET_AGENTS"))
        discover = None
        if os.getenv("AGENTKIT_FLEET_DISCOVER", "").lower() in ("1", "true", "yes"):
            from agentkit.hosting.clients import get_credential

            credential = get_credential(settings)
            subs = [s for s in os.getenv("AGENTKIT_FLEET_SUBSCRIPTIONS", "").split(",") if s.strip()]

            async def arm_token() -> str:
                return (await credential.get_token("https://management.azure.com/.default")).token

            async def discover() -> list[FleetAgent]:  # type: ignore[misc]
                return await discover_agents(arm_token, subscriptions=subs or None,
                                             environment=None if os.getenv("AGENTKIT_FLEET_ALL_ENVIRONMENTS") else settings.environment)
        registry = FleetRegistry(agents, discover)
    if logs is None and os.getenv("AGENTKIT_CONSOLE_LOGS_RESOURCE"):
        from agentkit.hosting.clients import get_credential

        credential = get_credential(settings)
        if credential is not None:
            logs = AzureLogsClient.for_resource(os.environ["AGENTKIT_CONSOLE_LOGS_RESOURCE"], credential)
    service = FleetService(registry, token_for or _token_for(settings), logs=logs, client=client,
                           caller_header=parse_caller_header(os.getenv("AGENTKIT_FLEET_CALLER_HEADER")))

    app = FastAPI(title=title, version=settings.service_version)
    app.add_middleware(_JsonOnly)
    app.state.fleet = service

    def check(request: Request) -> str:
        user = request.headers.get(settings.user_header) or request.headers.get(settings.user_fallback_header or "")
        if settings.require_user and not user:
            raise HTTPException(401, f"missing {settings.user_header}")
        if role and role not in roles_from_principal(request.headers.get(settings.principal_claims_header)):
            raise HTTPException(403, f"the fleet view requires the '{role}' role")
        return user or ""

    index = _bundle().joinpath("index.html").read_text(encoding="utf-8")
    page = (index.replace("__AGENTKIT_API__", "/v1/console").replace("__AGENTKIT_TITLE__", _html_attr(title))
            .replace('<meta name="agentkit-mode" content="agent" />', '<meta name="agentkit-mode" content="fleet" />'))

    async def console_page() -> Any:
        from fastapi.responses import HTMLResponse

        return HTMLResponse(page, headers={**_SECURITY_HEADERS, "Cache-Control": "no-cache"})

    async def console_asset(name: str) -> Any:
        from fastapi.responses import Response

        if "/" in name or "\\" in name or name.startswith("."):
            raise HTTPException(404)
        item = _bundle().joinpath("assets", name)
        if not item.is_file():
            raise HTTPException(404)
        media = _ASSET_TYPES.get(Path(name).suffix, "application/octet-stream")
        return Response(item.read_bytes(), media_type=media,
                        headers={**_SECURITY_HEADERS, "Cache-Control": "public, max-age=31536000, immutable"})

    async def console_route(rest: str) -> Any:
        if rest.startswith("assets/") or "." in rest.rsplit("/", 1)[-1]:
            raise HTTPException(404)
        return await console_page()

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict[str, str]:
        return {"status": "ready", "agent": "", "version": settings.service_version}

    @app.get("/", include_in_schema=False)
    async def root() -> Any:
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/console")

    app.add_api_route("/console", console_page, methods=["GET"], include_in_schema=False)
    app.add_api_route("/console/assets/{name}", console_asset, methods=["GET"], include_in_schema=False)
    app.add_api_route("/console/{rest:path}", console_route, methods=["GET"], include_in_schema=False)

    @app.get("/v1/fleet/me", tags=["Fleet"])
    async def me(request: Request) -> JSONResponse:
        user = check(request)
        return JSONResponse({"user": user or None, "title": title, "environment": settings.environment,
                             "kit_version": _kit_version()}, headers=_API_HEADERS)

    @app.get("/v1/fleet/agents", tags=["Fleet"], summary="Every registered agent: health, posture, gate, deploy")
    async def agents(request: Request) -> JSONResponse:
        check(request)
        return JSONResponse(await service.agents(), headers=_API_HEADERS)

    @app.get("/v1/fleet/traffic", tags=["Fleet"], summary="Runs, errors, latency and tokens per agent")
    async def traffic(request: Request, range: str = "24h") -> JSONResponse:  # noqa: A002
        check(request)
        if range not in RANGES:
            raise HTTPException(400, f"range must be one of {', '.join(RANGES)}")
        return JSONResponse(await service.traffic(range), headers=_API_HEADERS)

    return app


def _kit_version() -> str | None:
    from importlib import metadata

    try:
        return metadata.version("agentkit-channels")
    except metadata.PackageNotFoundError:
        return None


def __getattr__(name: str) -> Any:  # ``uvicorn agentkit.channels.fleet:app`` builds the app on first access
    if name == "app":
        globals()["app"] = create_fleet_app()
        return globals()["app"]
    raise AttributeError(name)

