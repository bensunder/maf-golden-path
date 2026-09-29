"""The VPS platform service: Create agent in the console builds and launches a real agent.

It sits between the sign-in proxy and the agents (``agentctl.py platform`` wires it in):

* **Router.** ``/agents/<name>/...`` goes to that agent, ``/fleet/...`` to the fleet view, everything else to
  the sample agent. It tells each one where it's mounted (``X-Agentkit-Prefix``) so its console and chat link
  correctly, and passes on the user oauth2-proxy signed in (``X-Forwarded-Email``).
* **Platform API** (``/v1/platform``). Lists the agents on this server and, for the admins in
  ``PLATFORM_ADMINS``, creates one: generate it from the kit's template (copier, from this checkout), register
  it (``agentctl.py add --internal``), build it (the build runs its evals as the quality gate), start it, wait
  until it's ready. Or removes one (containers and registry entry; its folder is kept).

It drives Docker on this host, so: it answers only the sign-in proxy (``PLATFORM_TRUSTED_PEER``), creating and
removing need an admin, inputs are validated strictly and passed as arguments (never through a shell), one
build runs at a time, and a failed build is rolled back.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from connectors import (CONNECTOR_NAME, Activity, ConnectorError, Store, Vault, check_url, policy_of,  # noqa: E402
                        create_gateway, list_tools, public_view, validate_entry)
from agentcalls import MAX_CALLS as MAX_CALLS_LIMIT, MAX_DEPTH as MAX_DEPTH_LIMIT, CallLog, install_agent_calls  # noqa: E402
from security import DELEGATION_HEADER, NONCE_HEADER, SIGNATURE_HEADER, SIGNED_AT_HEADER, delegation_key, mint, signed_headers  # noqa: E402
from templates import TEMPLATE_ID, TemplateError, apply_template, library_view, load_catalog, public, template_body  # noqa: E402

HOME = Path(os.getenv("PLATFORM_HOME", "/opt/maf-golden-path"))
VPS = HOME / "deploy" / "vps"
AGENTS_DIR = Path(os.getenv("PLATFORM_AGENTS_DIR", "/opt/agents"))
TEMPLATES_DIR = HOME / "agent-templates"  # template libraries for Create agent (agent-templates/README.md)
ADMINS = {a.strip().lower() for a in os.getenv("PLATFORM_ADMINS", "").split(",") if a.strip()}
PUBLIC_HOST = os.getenv("PLATFORM_PUBLIC_HOST", "")
TRUSTED_PEER = os.getenv("PLATFORM_TRUSTED_PEER", "auth")
USER_HEADER = "x-forwarded-email"
PREFIX_HEADER = "x-agentkit-prefix"
PLATFORM_HEADER = "x-agentkit-platform"  # marks what this router forwards: the console then offers Create agent
SAMPLE = os.getenv("PLATFORM_SAMPLE_UPSTREAM", "http://agent:8000")
FLEET = os.getenv("PLATFORM_FLEET_UPSTREAM", "http://fleet:8000")
UPSTREAM = os.getenv("PLATFORM_AGENT_UPSTREAM", "http://agent-{name}:8000")  # tests point it elsewhere
BUILD_TIMEOUT = float(os.getenv("PLATFORM_BUILD_TIMEOUT", "1800"))
READY_TIMEOUT = float(os.getenv("PLATFORM_READY_TIMEOUT", "180"))

# used with fullmatch: a trailing newline must not pass ("$" alone would allow one)
_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}[a-z0-9]")
_TITLE = re.compile(r"[A-Za-z][A-Za-z0-9 .,()&+-]{1,58}[A-Za-z0-9).]")
_TEXT = re.compile(r"[A-Za-z0-9 .,;:!?()&+/%#@-]{0,240}")  # no quotes, braces or backslashes: it lands in code
_TEAM = re.compile(r"[a-z][a-z0-9-]{1,30}")
FRAMEWORKS = {"maf", "langgraph"}  # Microsoft Agent Framework, or a LangGraph graph run on the same host
RESERVED = {"agent", "auth", "redis", "fleet", "platform", "www", "api", "console", "chat", "v1", "sample"}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b-\x1f\x7f]")
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "host", "content-length"}
# agents get the signed-in user (X-Forwarded-Email) and nothing that could be replayed: not the sign-in cookie,
# not credentials, not the router's own markers as a browser sent them
_NOT_FORWARDED = {"cookie", "authorization", "x-agentkit-prefix", "x-agentkit-platform",
                  DELEGATION_HEADER, SIGNATURE_HEADER, SIGNED_AT_HEADER, NONCE_HEADER}
MAX_FORWARDED_BODY = 8 * 1024 * 1024  # what an agent verifies a signature over (chat and approval requests are small)
DELEGATION_SECONDS = 600  # a person's request, and every agent call made for it, must finish within this
DECIDE_SECONDS = 120  # the request in which the person approves: its decision must reach the other agent quickly
MAX_JOBS = 50
_API_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def slugify(title: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", title.lower())).strip("-")[:32].strip("-")


# ------------------------------------------------------------------------------------------ registry
def registry() -> dict:
    path = VPS / "agents.yaml"
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None) or {}
    data.setdefault("agents", [])
    return data


def assignments() -> dict[str, dict]:
    """Each agent's connector token and connectors (agents.yaml; "sample" is the sample agent)."""
    data = registry()
    out = {a["name"]: {"token": a.get("connector_token"), "connectors": a.get("connectors") or []}
           for a in data["agents"] if isinstance(a, dict) and "name" in a}
    sample = data.get("sample") or {}
    out["sample"] = {"token": sample.get("connector_token"), "connectors": sample.get("connectors") or []}
    return out


SECRET = os.getenv("PLATFORM_SECRET_KEY") or ""
STORE = Store(VPS / "connectors.json", Vault(SECRET or None))
ACTIVITY = Activity()
CALLS = CallLog()  # agent-to-agent calls
CATALOG_LOCK = asyncio.Lock()  # one change to connectors.json at a time


def agent_names() -> set[str]:
    return {a["name"] for a in registry()["agents"] if isinstance(a, dict) and "name" in a}


# ------------------------------------------------------------------------------------------ jobs
class Job:
    def __init__(self, name: str, title: str, user: str):
        self.id = uuid.uuid4().hex[:12]
        self.name, self.title, self.user = name, title, user
        self.state = "queued"  # queued, generating, registering, building, starting, ready, failed
        self.error: str | None = None
        self.failed_step: str | None = None
        self.log: deque[str] = deque(maxlen=400)
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.template: str | None = None
        self.framework = "maf"

    def view(self, log: bool = True) -> dict:
        body = {"id": self.id, "name": self.name, "title": self.title, "created_by": self.user, "state": self.state,
                "error": self.error, "failed_step": self.failed_step, "created_at": self.created_at, "finished_at": self.finished_at,
                "path": f"/agents/{self.name}", "template": self.template, "framework": self.framework}
        if log:
            body["log"] = list(self.log)
        return body


class Builder:
    """Runs one creation at a time: each step is a command with fixed arguments, logged as it runs."""

    def __init__(self, runner=None):
        self.jobs: dict[str, Job] = {}
        self.lock = asyncio.Lock()
        self.run = runner or self._run
        self._tasks: set[asyncio.Task] = set()

    def _add(self, job: Job) -> None:
        self.jobs[job.id] = job
        finished = [j for j in self.jobs.values() if j.finished_at]
        for old in sorted(finished, key=lambda j: j.finished_at or 0)[:-MAX_JOBS] if len(finished) > MAX_JOBS else []:
            self.jobs.pop(old.id, None)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)  # asyncio keeps only weak references to tasks
        task.add_done_callback(self._tasks.discard)

    async def _run(self, job: Job, argv: list[str], cwd: Path, timeout: float = 600) -> None:
        job.log.append("$ " + " ".join(argv))
        proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"})

        async def pump() -> None:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = _ANSI.sub("", raw.decode("utf-8", "replace")).rstrip()
                if line:
                    job.log.append(line[:500])

        try:
            await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 30)
            raise RuntimeError(f"{argv[0]} took longer than {int(timeout)} s") from None
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(argv[:3])} failed (exit {proc.returncode})")

    def start(self, name: str, title: str, description: str, team: str, knowledge: bool, user: str,
              connectors: list[str] | None = None, template: str | None = None, framework: str = "maf") -> Job:
        job = Job(name, title, user)
        job.template = template
        job.framework = framework
        self._add(job)
        self._spawn(self._create(job, description, team, knowledge, connectors or [], template, framework))
        return job

    def start_assign(self, name: str, connectors: list[str], user: str) -> Job:
        """Give an agent a new set of connectors: new settings and a restart, no rebuild."""
        job = Job(name, name, user)
        job.state = "connecting"
        self._add(job)
        self._spawn(self._assign(job, connectors))
        return job

    def start_peers(self, name: str, peers: list[str], user: str) -> Job:
        """Set which agents an agent may call: new settings and a restart, no rebuild."""
        job = Job(name, name, user)
        job.state = "connecting"
        self._add(job)
        self._spawn(self._assign(job, peers, command="peers"))
        return job

    def start_refresh(self, agents: list[str], user: str, label: str) -> Job:
        """Restart the agents using a connector whose allowed tools changed, so they pick up the change."""
        job = Job(label, label, user)
        job.state = "connecting"
        self._add(job)
        self._spawn(self._refresh(job, agents))
        return job

    @staticmethod
    def service_of(name: str) -> str:
        return "agent" if name == "sample" else f"agent-{name}"

    async def _assign(self, job: Job, connectors: list[str], command: str = "connect") -> None:
        async with self.lock:
            try:
                await self.run(job, [sys.executable, "agentctl.py", command, job.name, ",".join(connectors) or "none"], VPS, 60)
                await self.run(job, ["docker", "compose", "up", "-d", self.service_of(job.name)], VPS, 300)
                await self.wait_ready(job, SAMPLE if job.name == "sample" else None)
                job.state = "ready"
            except Exception as exc:
                job.failed_step = job.state
                job.state, job.error = "failed", str(exc)[:300]
            finally:
                job.finished_at = time.time()

    async def _refresh(self, job: Job, agents: list[str]) -> None:
        async with self.lock:
            try:
                await self.run(job, [sys.executable, "agentctl.py", "render"], VPS, 60)
                if agents:
                    await self.run(job, ["docker", "compose", "up", "-d", *[self.service_of(a) for a in agents]], VPS, 300)
                job.state = "ready"
            except Exception as exc:
                job.failed_step = job.state
                job.state, job.error = "failed", str(exc)[:300]
            finally:
                job.finished_at = time.time()

    async def _create(self, job: Job, description: str, team: str, knowledge: bool, connectors: list[str],
                      template: str | None = None, framework: str = "maf") -> None:
        async with self.lock:
            folder = AGENTS_DIR / job.name
            generated = registered = False
            try:
                if job.name in agent_names() or folder.exists():
                    raise RuntimeError(f"{job.name} already exists")
                job.state = "generating"
                generated = True  # from here on, the folder is this job's
                await self.run(job, ["copier", "copy", "--trust", "--defaults", "--vcs-ref", "HEAD",
                                     "--data", f"project_name={job.title}", "--data", f"project_slug={job.name}",
                                     "--data", f"package_name={job.name.replace('-', '_')}",
                                     "--data", f"description={description}",
                                     "--data", f"team={team}", "--data", "enable_web_chat=true",
                                     "--data", "enable_teams=false", "--data", f"enable_knowledge={str(knowledge).lower()}",
                                     "--data", f"framework={framework}",
                                     str(HOME), str(folder)], VPS, 300)
                if template:
                    self.apply(job, folder, template)
                job.state = "registering"
                registered = True  # agentctl may have written agents.yaml even if it then fails
                await self.run(job, [sys.executable, "agentctl.py", "add", job.name, str(folder), "--internal",
                                     "--by", job.user], VPS, 60)
                if connectors:
                    await self.run(job, [sys.executable, "agentctl.py", "connect", job.name, ",".join(connectors)], VPS, 60)
                job.state = "building"  # the build runs the agent's offline evals: a failing gate stops here
                await self.run(job, ["docker", "compose", "up", "-d", "--build", f"agent-{job.name}"], VPS, BUILD_TIMEOUT)
                job.state = "starting"
                await self.wait_ready(job)
                await self.refresh_fleet(job)
                job.state = "ready"
                job.log.append(f"ready: https://{PUBLIC_HOST}/agents/{job.name}/console")
            except Exception as exc:
                job.failed_step = job.state
                job.state, job.error = "failed", str(exc)[:300]
                job.log.append(f"failed: {job.error}")
                await self.rollback(job, folder, generated, registered)
            finally:
                job.finished_at = time.time()

    def apply(self, job: Job, folder: Path, template: str) -> None:
        catalog = load_catalog(TEMPLATES_DIR)
        entry = catalog["templates"].get(template)
        library = next((lib for lib in catalog["libraries"] if entry and lib["name"] == entry["library"]), None)
        if entry is None or library is None:
            raise RuntimeError(f"the template {template} is no longer installed")
        job.log.append(f"$ apply template {template}   ({library['title']}, {library['license'] or 'no license given'})")
        for change in apply_template(folder, job.name.replace("-", "_"), entry, library):
            job.log.append(f"  wrote {change}")

    async def wait_ready(self, job: Job, base: str | None = None) -> None:
        deadline = time.monotonic() + READY_TIMEOUT
        await asyncio.sleep(1)  # a restarted container answers for a moment with its old process
        async with httpx.AsyncClient(timeout=5) as http:
            while time.monotonic() < deadline:
                with contextlib.suppress(httpx.HTTPError):
                    if (await http.get((base or UPSTREAM.format(name=job.name)) + "/readyz")).status_code == 200:
                        return
                await asyncio.sleep(2)
        raise RuntimeError(f"agent-{job.name} didn't become ready within {int(READY_TIMEOUT)} s")

    async def refresh_fleet(self, job: Job) -> None:
        """The fleet lists agents from its settings, so it restarts with the new list. A failure here only
        means the fleet view is out of date: the agent itself is fine."""
        if (registry().get("fleet") or None) is None:
            return
        try:
            await self.run(job, ["docker", "compose", "up", "-d", "fleet"], VPS, 300)
        except Exception as exc:
            job.log.append(f"warning: the fleet view didn't restart ({exc}); run `docker compose up -d fleet`")

    async def rollback(self, job: Job, folder: Path, generated: bool, registered: bool) -> None:
        """Leave nothing half-made, so the same name can be tried again; keep the folder for a look.
        Each step is tried on its own: one failing doesn't skip the others."""
        if registered:
            with contextlib.suppress(Exception):
                await self.run(job, ["docker", "compose", "rm", "-sf", f"agent-{job.name}"], VPS, 120)
            with contextlib.suppress(Exception):
                if job.name in agent_names():
                    await self.run(job, [sys.executable, "agentctl.py", "remove", job.name], VPS, 60)
        with contextlib.suppress(Exception):
            if generated and folder.exists():
                failed = AGENTS_DIR / ".failed"
                failed.mkdir(exist_ok=True)
                target = failed / f"{job.name}-{int(time.time())}"
                shutil.move(str(folder), str(target))
                job.log.append(f"kept the generated files in {target}")

    def start_removal(self, name: str, user: str) -> Job:
        job = Job(name, name, user)
        job.state = "removing"
        self._add(job)
        self._spawn(self._remove(job))
        return job

    async def _remove(self, job: Job) -> None:
        name = job.name
        async with self.lock:
            try:
                job.state = "removing"
                await self.run(job, ["docker", "compose", "rm", "-sf", f"agent-{name}"], VPS, 120)
                await self.run(job, [sys.executable, "agentctl.py", "remove", name], VPS, 60)
                await self.refresh_fleet(job)
                folder = AGENTS_DIR / name
                if folder.is_dir():  # kept, but out of the way: the name can be used again
                    kept = AGENTS_DIR / ".removed" / f"{name}-{int(time.time())}"
                    kept.parent.mkdir(exist_ok=True)
                    shutil.move(str(folder), str(kept))
                    job.log.append(f"its files are in {kept}")
                job.state = "removed"
            except Exception as exc:
                job.failed_step = "removing"
                job.state, job.error = "failed", str(exc)[:300]
            finally:
                job.finished_at = time.time()


# ------------------------------------------------------------------------------------------ app
_PEERS: dict[str, tuple[float, set[str]]] = {}


async def _peer_ips(host: str, fresh: bool = False) -> set[str]:
    """The sign-in proxy's addresses on the Docker network (cached 10 s: containers move). Fails closed."""
    cached = _PEERS.get(host)
    if cached and not fresh and time.monotonic() - cached[0] < 10:
        return cached[1]
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
        ips = {info[4][0] for info in infos}
    except OSError:
        ips = set()
    _PEERS[host] = (time.monotonic(), ips)
    return ips


async def _is_peer(host: str, address: str) -> bool:
    if address in await _peer_ips(host):
        return True
    return address in await _peer_ips(host, fresh=True)  # the proxy may just have restarted with a new address


def create_app(builder: Builder | None = None, *, trusted_peer: str | None = TRUSTED_PEER,
               client: httpx.AsyncClient | None = None) -> FastAPI:
    app = FastAPI(title="agentkit platform", docs_url=None, redoc_url=None, openapi_url=None)
    builder = builder or Builder()
    app.state.builder = builder
    # no pool cap: every open chat stream (SSE) holds a connection for as long as it runs
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None), follow_redirects=False,
                                       limits=httpx.Limits(max_connections=None, max_keepalive_connections=50))

    @app.middleware("http")
    async def only_from_the_sign_in_proxy(request: Request, call_next):
        # the agents share this Docker network and trust X-Forwarded-Email; so does this service, which is
        # why it only serves the sign-in proxy, never another container
        if request.url.path != "/healthz" and trusted_peer:
            peer = request.client.host if request.client else ""
            if not await _is_peer(trusted_peer, peer):
                return JSONResponse({"detail": "only the sign-in proxy may call the platform"}, status_code=403)
        return await call_next(request)

    def user_of(request: Request) -> str:
        user = (request.headers.get(USER_HEADER) or "").strip().lower()
        if not user:
            raise HTTPException(401, "not signed in")
        return user

    def admin(request: Request) -> str:
        user = user_of(request)
        origin = (request.headers.get("origin") or "").lower()
        if PUBLIC_HOST and origin != f"https://{PUBLIC_HOST.lower()}":  # browsers send Origin on POST and DELETE
            raise HTTPException(403, "cross-site request refused")
        if not ADMINS:
            raise HTTPException(403, "no platform admins are configured (agentctl.py platform --admins ...)")
        if user not in ADMINS:
            raise HTTPException(403, "only platform admins can create or remove agents")
        return user

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/v1/platform")
    async def info(request: Request) -> JSONResponse:
        user = user_of(request)
        data = registry()
        return JSONResponse({"platform": True, "user": user, "is_admin": user in ADMINS,
                             "admins_configured": bool(ADMINS), "public_host": PUBLIC_HOST,
                             "fleet": bool(data.get("fleet")), "agents_dir": str(AGENTS_DIR) if user in ADMINS else None,
                             "running_job": next((j.view(log=False) for j in builder.jobs.values()
                                                  if j.state not in ("ready", "failed", "removed")), None)},
                            headers=_API_HEADERS)

    async def probe(url: str) -> dict:
        try:
            r = await http.get(url + "/readyz", timeout=4)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            return {"status": "ready" if r.status_code == 200 else "not_ready", "version": body.get("version")}
        except (httpx.HTTPError, ValueError):
            return {"status": "unreachable", "version": None}

    @app.get("/v1/platform/agents")
    async def agents(request: Request) -> JSONResponse:
        user_of(request)
        data = registry()
        rows = [{"name": "sample", "title": "Order Status Agent (sample)", "path": "", "internal": True,
                 "created_by": None, "created_at": None, "service": "order-status-agent", "url": SAMPLE}]
        rows[0]["connectors"] = (data.get("sample") or {}).get("connectors") or []
        rows[0]["peers"] = (data.get("sample") or {}).get("peers") or []
        for a in data["agents"]:
            internal = bool(a.get("internal"))
            rows.append({"connectors": a.get("connectors") or [], "peers": a.get("peers") or [], "name": a["name"], "title": a.get("title") or a.get("service") or a["name"],
                         "path": f"/agents/{a['name']}", "internal": internal,
                         "host": None if internal else a.get("host"), "service": a.get("service"),
                         "created_by": a.get("created_by"), "created_at": a.get("created_at"),
                         "url": UPSTREAM.format(name=a["name"])})
        probes = await asyncio.gather(*(probe(r.pop("url")) for r in rows))
        for row, p in zip(rows, probes):
            row.update(p)
        building = [j.view(log=False) for j in builder.jobs.values() if j.state not in ("ready", "failed", "removed")]
        return JSONResponse({"agents": rows, "jobs": building}, headers=_API_HEADERS)

    @app.post("/v1/platform/agents", status_code=202)
    async def create(request: Request) -> JSONResponse:
        user = admin(request)
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "send JSON")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "invalid JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(422, "send a JSON object")
        title = str(body.get("title") or "").strip()
        name = str(body.get("name") or slugify(title)).strip()
        description = str(body.get("description") or "").strip()
        team = str(body.get("team") or name).strip().lower()
        knowledge = body.get("knowledge", False)
        connectors = body.get("connectors") or []
        template = body.get("template") or None
        framework = body.get("framework") or "maf"
        problems = []
        if framework not in FRAMEWORKS:
            problems.append("framework: maf or langgraph")
        if template is not None and not (isinstance(template, str) and TEMPLATE_ID.fullmatch(template)
                                         and template in load_catalog(TEMPLATES_DIR)["templates"]):
            problems.append("template: the id of an installed template")
        if not isinstance(connectors, list) or not all(isinstance(c, str) and CONNECTOR_NAME.fullmatch(c) for c in connectors) \
                or any(c not in STORE.load() for c in connectors):
            problems.append("connectors: names of connectors that exist")
        if not _TITLE.fullmatch(title):
            problems.append("title: 3–60 letters, digits, spaces and . , ( ) & + -")
        if not _NAME.fullmatch(name) or name in RESERVED:
            problems.append("name: 2–32 lowercase letters, digits or dashes (not a reserved word)")
        if not _TEXT.fullmatch(description):
            problems.append("description: up to 240 characters, no quotes, braces or backslashes")
        if not _TEAM.fullmatch(team):
            problems.append("team: 2–31 lowercase letters, digits or dashes")
        if not isinstance(knowledge, bool):
            problems.append("knowledge: true or false")
        if problems:
            raise HTTPException(422, "; ".join(problems))
        if name in agent_names() or (AGENTS_DIR / name).exists():
            raise HTTPException(409, f"an agent named {name} already exists")
        if any(j.name == name and j.state not in ("ready", "failed", "removed") for j in builder.jobs.values()):
            raise HTTPException(409, f"{name} is already being created")
        job = builder.start(name, title, description, team, knowledge, user, connectors, template, framework)
        return JSONResponse(job.view(), status_code=202, headers=_API_HEADERS)

    @app.get("/v1/platform/jobs/{job_id}")
    async def job(job_id: str, request: Request) -> JSONResponse:
        user = user_of(request)
        found = builder.jobs.get(job_id)
        if not found or (user not in ADMINS and user != found.user):  # build logs are for admins
            raise HTTPException(404, "no such job")
        return JSONResponse(found.view(), headers=_API_HEADERS)

    @app.delete("/v1/platform/agents/{name}", status_code=202)
    async def remove(name: str, request: Request) -> JSONResponse:
        user = admin(request)
        if request.headers.get("x-agentkit-confirm") != name:
            raise HTTPException(400, "confirm with the header X-Agentkit-Confirm: <name>")
        if name not in agent_names():
            raise HTTPException(404, f"no agent named {name}")
        entry = next(a for a in registry()["agents"] if a["name"] == name)
        if not entry.get("internal"):
            raise HTTPException(409, f"{name} has its own host name: remove it with agentctl.py on the server")
        if any(j.name == name and j.state not in ("ready", "failed", "removed") for j in builder.jobs.values()):
            raise HTTPException(409, f"{name} is being changed right now")
        job = builder.start_removal(name, user)
        return JSONResponse(job.view(), status_code=202, headers=_API_HEADERS)

    # ------------------------------------------------------------------ connectors
    def used_by(name: str) -> list[str]:
        return sorted(a for a, v in assignments().items() if name in (v.get("connectors") or []))

    async def json_body(request: Request) -> dict:
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "send JSON")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "invalid JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(422, "send a JSON object")
        return body

    def busy() -> None:
        if any(j.state not in ("ready", "failed", "removed") for j in builder.jobs.values()):
            raise HTTPException(409, "another change is running: try again when it finishes")

    @app.get("/v1/platform/templates")
    async def templates(request: Request) -> JSONResponse:
        user_of(request)
        catalog = load_catalog(TEMPLATES_DIR)
        return JSONResponse({"libraries": [library_view(lib) for lib in catalog["libraries"]]}, headers=_API_HEADERS)

    @app.get("/v1/platform/templates/{template_id:path}")
    async def template_detail(template_id: str, request: Request) -> JSONResponse:
        user_of(request)
        catalog = load_catalog(TEMPLATES_DIR)
        entry = catalog["templates"].get(template_id) if TEMPLATE_ID.fullmatch(template_id) else None
        if entry is None:
            raise HTTPException(404, "no such template")
        library = next(lib for lib in catalog["libraries"] if lib["name"] == entry["library"])
        return JSONResponse({**public(entry), "body": template_body(entry), "library_title": library["title"],
                             "source": library["source"], "commit": library["commit"], "license": library["license"],
                             "rules": library["rules"], "evals": library["evals"]}, headers=_API_HEADERS)

    @app.get("/v1/platform/connectors")
    async def connectors_list(request: Request) -> JSONResponse:
        user = user_of(request)
        items = [public_view(n, e, used_by(n), admin=user in ADMINS) for n, e in sorted(STORE.load().items())]
        return JSONResponse({"connectors": items, "can_manage": user in ADMINS, "vault": STORE.vault.ready,
                             "vault_error": STORE.vault.error if user in ADMINS else None}, headers=_API_HEADERS)

    @app.post("/v1/platform/connectors/test")
    async def connectors_test(request: Request) -> JSONResponse:
        admin(request)
        body = await json_body(request)
        try:
            if body.get("name") and not body.get("url"):  # an existing connector, with its stored credential
                entry = STORE.get(str(body["name"]))
                if not entry:
                    raise HTTPException(404, "no such connector")
                url, headers = entry["url"], STORE.credential_headers(entry)
            else:
                probe = validate_entry("probe", {**body, "tools": []})
                secret = str(body.get("secret") or "")
                url = probe.get("url") or ""
                headers = ({"authorization": f"Bearer {secret}"} if probe["auth"] == "bearer" and secret else
                           {probe["header"]: secret} if probe["auth"] == "header" and secret else {})
            tools = await list_tools(url, headers)
        except ConnectorError as exc:
            raise HTTPException(422, str(exc)) from None
        return JSONResponse({"tools": tools}, headers=_API_HEADERS)

    @app.post("/v1/platform/connectors", status_code=201)
    async def connectors_add(request: Request) -> JSONResponse:
        user = admin(request)
        body = await json_body(request)
        name = str(body.get("name") or "").strip()
        try:
            entry = validate_entry(name, body)
            await check_url(entry.get("url") or "")
            secret = str(body.get("secret") or "")
            if entry["auth"] != "none":
                if not secret:
                    raise ConnectorError("secret: the token or API key")
                entry["secret"] = STORE.vault.seal(secret)
        except ConnectorError as exc:
            raise HTTPException(422, str(exc)) from None
        entry.update(created_by=user, created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        async with CATALOG_LOCK:
            current = STORE.load()
            if name in current:
                raise HTTPException(409, f"a connector named {name} already exists")
            current[name] = entry
            STORE.save(current)
        return JSONResponse(public_view(name, entry, [], admin=True), status_code=201, headers=_API_HEADERS)

    @app.patch("/v1/platform/connectors/{name}")
    async def connectors_update(name: str, request: Request) -> JSONResponse:
        user = admin(request)
        body = await json_body(request)
        async with CATALOG_LOCK:
            current = STORE.load()
            if name not in current:
                raise HTTPException(404, f"no connector named {name}")
            before = (current[name].get("allowed"), current[name].get("approval"), current[name].get("title"))
            try:
                entry = validate_entry(name, {k: body[k] for k in ("title", "tools", "allowed", "approval") if k in body},
                                       existing=current[name])
                if body.get("secret"):
                    entry["secret"] = STORE.vault.seal(str(body["secret"]))
            except ConnectorError as exc:
                raise HTTPException(422, str(exc)) from None
            agents = used_by(name)
            changed = before != (entry.get("allowed"), entry.get("approval"), entry.get("title"))
            if agents and changed:
                busy()  # before saving: new tool rules must never be live without the agents restarting
            current[name] = entry
            STORE.save(current)
        job = builder.start_refresh(agents, user, f"connector-{name}").view() if agents and changed else None
        return JSONResponse({**public_view(name, entry, agents, admin=True), "job": job}, headers=_API_HEADERS)

    @app.delete("/v1/platform/connectors/{name}")
    async def connectors_remove(name: str, request: Request) -> JSONResponse:
        admin(request)
        if request.headers.get("x-agentkit-confirm") != name:
            raise HTTPException(400, "confirm with the header X-Agentkit-Confirm: <name>")
        if name not in STORE.load():
            raise HTTPException(404, f"no connector named {name}")
        agents = used_by(name)
        if agents:
            raise HTTPException(409, f"still used by {', '.join(agents)}: take it off those agents first")
        async with CATALOG_LOCK:
            current = STORE.load()
            current.pop(name, None)
            STORE.save(current)
        return JSONResponse({"removed": name}, headers=_API_HEADERS)

    @app.get("/v1/platform/connectors/{name}/activity")
    async def connectors_activity(name: str, request: Request) -> JSONResponse:
        user = user_of(request)
        if user not in ADMINS:
            raise HTTPException(403, "only platform admins can see connector activity")
        return JSONResponse({"activity": ACTIVITY.for_connector(name)}, headers=_API_HEADERS)

    @app.put("/v1/platform/agents/{name}/connectors", status_code=202)
    async def agent_connectors(name: str, request: Request) -> JSONResponse:
        user = admin(request)
        body = await json_body(request)
        wanted = body.get("connectors")
        known = STORE.load()
        if not isinstance(wanted, list) or not all(isinstance(c, str) and c in known for c in wanted):
            raise HTTPException(422, "connectors: names of connectors that exist")
        if name != "sample" and name not in agent_names():
            raise HTTPException(404, f"no agent named {name}")
        busy()
        job = builder.start_assign(name, sorted(set(wanted)), user)
        return JSONResponse(job.view(), status_code=202, headers=_API_HEADERS)

    # ------------------------------------------------------------------ agent network
    def callable_agents() -> dict[str, dict]:
        data = registry()
        out = {"sample": {"title": "Order Status Agent (sample)", "peers": (data.get("sample") or {}).get("peers") or []}}
        for a in data["agents"]:
            if isinstance(a, dict) and a.get("internal"):
                out[a["name"]] = {"title": a.get("title") or a.get("service") or a["name"], "peers": a.get("peers") or []}
        return out

    @app.get("/v1/platform/network")
    async def network(request: Request) -> JSONResponse:
        user = user_of(request)
        agents = callable_agents()
        edges = [{"from": a, "to": p} for a, v in agents.items() for p in v["peers"] if p in agents]
        calls = CALLS.recent(None if user in ADMINS else user)
        return JSONResponse({"agents": [{"name": n, "title": v["title"], "peers": v["peers"]} for n, v in agents.items()],
                             "edges": edges, "calls": calls, "all_calls": user in ADMINS,
                             "limits": {"depth": MAX_DEPTH_LIMIT, "calls_per_request": MAX_CALLS_LIMIT}},
                            headers=_API_HEADERS)

    @app.put("/v1/platform/agents/{name}/peers", status_code=202)
    async def agent_peers(name: str, request: Request) -> JSONResponse:
        user = admin(request)
        body = await json_body(request)
        wanted = body.get("peers")
        agents = callable_agents()
        if name not in agents:
            raise HTTPException(404, f"no agent named {name} behind the platform (agents with their own host name can't join)")
        if not isinstance(wanted, list) or not all(isinstance(p, str) and p in agents and p != name for p in wanted):
            raise HTTPException(422, "peers: names of other agents on this server")
        busy()
        job = builder.start_peers(name, sorted(set(wanted)), user)
        return JSONResponse(job.view(), status_code=202, headers=_API_HEADERS)

    @app.api_route("/v1/platform/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def platform_other(rest: str) -> Response:  # never falls through to an agent
        raise HTTPException(404, "not a platform endpoint")

    # ------------------------------------------------------------------ router
    async def forward(request: Request, base: str, path: str, prefix: str, agent: str | None = None) -> Response:
        listed = {h.strip().lower() for h in request.headers.get("connection", "").split(",") if h.strip()}
        headers = [(k, v) for k, v in request.headers.items()
                   if k.lower() not in _HOP and k.lower() not in _NOT_FORWARDED and k.lower() not in listed]
        headers.append(("host", PUBLIC_HOST or request.headers.get("host", "localhost")))
        headers.append((PLATFORM_HEADER, "1"))
        if prefix:
            headers.append((PREFIX_HEADER, prefix))
        url = base + path + (("?" + request.url.query) if request.url.query else "")
        content: Any = request.stream()
        if agent and SECRET:
            # Signed for this agent: the method, path, user, delegation and body. The delegation token says whom
            # the request acts for; the agent passes it back if it calls another agent, and nothing else can.
            content = b""
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                declared = request.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_FORWARDED_BODY:
                    raise HTTPException(413, "request too large")
                chunks, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_FORWARDED_BODY:
                        raise HTTPException(413, "request too large")
                    chunks.append(chunk)
                content = b"".join(chunks)
            user = next((v for k, v in headers if k.lower() == USER_HEADER), "")
            # Only requests that can run the agent get a delegation (pages, assets and probes don't), and only the
            # one in which the person submits an approval may carry a decision on to another agent.
            if user and request.method == "POST":
                kind = "decide" if _is_approval(path, content) else "turn"
                lifetime = DECIDE_SECONDS if kind == "decide" else DELEGATION_SECONDS
                delegation = mint(delegation_key(SECRET), user=user, audience=agent, chain=[], root=uuid.uuid4().hex,
                                  expires=time.time() + lifetime, kind=kind)
                headers.append((DELEGATION_HEADER, delegation))
        upstream = http.build_request(request.method, url, headers=headers, content=content)
        if agent and SECRET:
            user = upstream.headers.get(USER_HEADER, "")
            upstream.headers.update(signed_headers(SECRET, agent, request.method, upstream.url.raw_path.decode("ascii"),
                                                   user, upstream.headers.get(DELEGATION_HEADER, ""), content))
        try:
            response = await http.send(upstream, stream=True)
        except httpx.HTTPError:
            return JSONResponse({"detail": "that agent isn't answering right now"}, status_code=502)
        async def body():
            try:
                if response.is_stream_consumed:  # already read (small responses from some transports)
                    yield response.content
                    return
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:  # also when the browser goes away mid-stream
                await response.aclose()

        def out(k: str, v: str) -> tuple[bytes, bytes]:
            if k.lower() == "location" and prefix and v.startswith("/") and not v.startswith("//") \
                    and not v.startswith(prefix + "/") and v != prefix:
                v = prefix + v  # an agent's own redirect, e.g. a trailing-slash fix, stays under its path
            return k.encode("latin-1"), v.encode("latin-1")

        streamed = StreamingResponse(body(), status_code=response.status_code)
        # every header as sent, repeated ones (Set-Cookie) included; the body is passed through as it arrives
        streamed.raw_headers = [out(k, v) for k, v in response.headers.multi_items()
                                if k.lower() not in _HOP or k.lower() == "content-length"]
        return streamed

    @app.api_route("/agents/{name}", methods=["GET"], include_in_schema=False)
    async def agent_root(name: str) -> Response:
        return Response(status_code=307, headers={"location": f"/agents/{name}/"})

    @app.api_route("/agents/{name}/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                   include_in_schema=False)
    async def agent_route(name: str, rest: str, request: Request) -> Response:
        if not _NAME.fullmatch(name) or name not in agent_names():  # only registered agents, never any container
            raise HTTPException(404, f"no agent named {name}")
        return await forward(request, UPSTREAM.format(name=name), "/" + rest, f"/agents/{name}", agent=name)

    @app.api_route("/fleet/{rest:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def fleet_route(rest: str, request: Request) -> Response:
        if not registry().get("fleet"):
            raise HTTPException(404, "the fleet view isn't on (agentctl.py fleet)")
        return await forward(request, FLEET, "/" + rest, "/fleet")

    @app.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                   include_in_schema=False)
    async def sample_route(rest: str, request: Request) -> Response:
        return await forward(request, SAMPLE, "/" + rest, "", agent="sample")

    return app


_APPROVALS_PATH = re.compile(r"/v1/sessions/[A-Za-z0-9_.:-]{1,128}/approvals")


def _is_approval(path: str, body: bytes) -> bool:
    """The person approving something: the JSON API's decision endpoint, or an AG-UI run resuming interrupts (how
    the web chat and the console send Approve), with at least one decision that approves. Rejections, and
    everything else, never carry a decision on to another agent."""
    if not body or not (_APPROVALS_PATH.fullmatch(path) or path == "/v1/agui"):
        return False
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    if path == "/v1/agui":
        entries = payload.get("resume")
        return isinstance(entries, list) and any(
            isinstance(e, dict) and e.get("status", "resolved") == "resolved" and isinstance(e.get("payload"), dict)
            and e["payload"].get("approved") is True for e in entries)
    decisions = payload.get("decisions")
    return isinstance(decisions, list) and any(isinstance(d, dict) and d.get("approved") is True for d in decisions)


def upstream_of(agent: str) -> str:
    return SAMPLE if agent == "sample" else UPSTREAM.format(name=agent)


def create_internal_gateway(client: httpx.AsyncClient | None = None) -> FastAPI:
    """Port 8001, for containers on the Docker network: connectors, agent-to-agent calls, the fleet's reads."""
    gateway = create_gateway(STORE, assignments, ACTIVITY, client)
    if SECRET:
        install_agent_calls(gateway, secret=SECRET, registry=registry, assignments=assignments, upstream=upstream_of,
                            is_fleet=lambda address: _is_peer("fleet", address), log=CALLS, client=client)
    return gateway


def serve() -> None:
    """The router and platform API on 8000 (for the sign-in proxy), the connector gateway on 8001 (for agents)."""
    import uvicorn

    async def main() -> None:
        public = uvicorn.Server(uvicorn.Config(create_app(), host="0.0.0.0", port=8000, log_level="info"))
        gateway = uvicorn.Server(uvicorn.Config(create_internal_gateway(), host="0.0.0.0", port=8001,
                                                log_level="warning"))
        await asyncio.gather(public.serve(), gateway.serve())

    asyncio.run(main())


if __name__ == "__main__":
    serve()


def __getattr__(name: str) -> Any:  # `uvicorn server:app` builds it on first use
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
