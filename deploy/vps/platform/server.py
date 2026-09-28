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

HOME = Path(os.getenv("PLATFORM_HOME", "/opt/maf-golden-path"))
VPS = HOME / "deploy" / "vps"
AGENTS_DIR = Path(os.getenv("PLATFORM_AGENTS_DIR", "/opt/agents"))
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
RESERVED = {"agent", "auth", "redis", "fleet", "platform", "www", "api", "console", "chat", "v1"}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b-\x1f\x7f]")
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "host", "content-length"}
# agents get the signed-in user (X-Forwarded-Email) and nothing that could be replayed: not the sign-in cookie,
# not credentials, not the router's own markers as a browser sent them
_NOT_FORWARDED = {"cookie", "authorization", "x-agentkit-prefix", "x-agentkit-platform"}
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

    def view(self, log: bool = True) -> dict:
        body = {"id": self.id, "name": self.name, "title": self.title, "created_by": self.user, "state": self.state,
                "error": self.error, "failed_step": self.failed_step, "created_at": self.created_at, "finished_at": self.finished_at,
                "path": f"/agents/{self.name}"}
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

    def start(self, name: str, title: str, description: str, team: str, knowledge: bool, user: str) -> Job:
        job = Job(name, title, user)
        self._add(job)
        self._spawn(self._create(job, description, team, knowledge))
        return job

    async def _create(self, job: Job, description: str, team: str, knowledge: bool) -> None:
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
                                     str(HOME), str(folder)], VPS, 300)
                job.state = "registering"
                registered = True  # agentctl may have written agents.yaml even if it then fails
                await self.run(job, [sys.executable, "agentctl.py", "add", job.name, str(folder), "--internal",
                                     "--by", job.user], VPS, 60)
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

    async def wait_ready(self, job: Job) -> None:
        deadline = time.monotonic() + READY_TIMEOUT
        async with httpx.AsyncClient(timeout=5) as http:
            while time.monotonic() < deadline:
                with contextlib.suppress(httpx.HTTPError):
                    if (await http.get(UPSTREAM.format(name=job.name) + "/readyz")).status_code == 200:
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
        for a in data["agents"]:
            internal = bool(a.get("internal"))
            rows.append({"name": a["name"], "title": a.get("title") or a.get("service") or a["name"],
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
        problems = []
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
        job = builder.start(name, title, description, team, knowledge, user)
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

    @app.api_route("/v1/platform/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def platform_other(rest: str) -> Response:  # never falls through to an agent
        raise HTTPException(404, "not a platform endpoint")

    # ------------------------------------------------------------------ router
    async def forward(request: Request, base: str, path: str, prefix: str) -> Response:
        listed = {h.strip().lower() for h in request.headers.get("connection", "").split(",") if h.strip()}
        headers = [(k, v) for k, v in request.headers.items()
                   if k.lower() not in _HOP and k.lower() not in _NOT_FORWARDED and k.lower() not in listed]
        headers.append(("host", PUBLIC_HOST or request.headers.get("host", "localhost")))
        headers.append((PLATFORM_HEADER, "1"))
        if prefix:
            headers.append((PREFIX_HEADER, prefix))
        url = base + path + (("?" + request.url.query) if request.url.query else "")
        upstream = http.build_request(request.method, url, headers=headers, content=request.stream())
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
        return await forward(request, UPSTREAM.format(name=name), "/" + rest, f"/agents/{name}")

    @app.api_route("/fleet/{rest:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def fleet_route(rest: str, request: Request) -> Response:
        if not registry().get("fleet"):
            raise HTTPException(404, "the fleet view isn't on (agentctl.py fleet)")
        return await forward(request, FLEET, "/" + rest, "/fleet")

    @app.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                   include_in_schema=False)
    async def sample_route(rest: str, request: Request) -> Response:
        return await forward(request, SAMPLE, "/" + rest, "")

    return app


def __getattr__(name: str) -> Any:  # `uvicorn server:app` builds it on first use
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
