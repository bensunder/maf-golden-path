"""deploy/vps/platform: the router in front of the agents, and Create agent (generate, register, build, start)."""

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

SERVER = Path(__file__).resolve().parents[2] / "deploy" / "vps" / "platform" / "server.py"
ADMIN = {"x-forwarded-email": "ben@contoso.example"}
OTHER = {"x-forwarded-email": "someone@contoso.example"}
SITE = {"origin": "https://agent.203-0-113-7.sslip.io"}  # browsers send it on POST and DELETE
ADMIN_W, OTHER_W = {**ADMIN, **SITE}, {**OTHER, **SITE}


@pytest.fixture
def platform(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("platform_server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = tmp_path / "kit"
    (home / "deploy" / "vps").mkdir(parents=True)
    monkeypatch.setattr(mod, "HOME", home)
    monkeypatch.setattr(mod, "VPS", home / "deploy" / "vps")
    monkeypatch.setattr(mod, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(mod, "ADMINS", {"ben@contoso.example"})
    monkeypatch.setattr(mod, "PUBLIC_HOST", "agent.203-0-113-7.sslip.io")
    (tmp_path / "agents").mkdir()
    return mod


def write_registry(mod, agents, fleet=None):
    (mod.VPS / "agents.yaml").write_text(yaml.safe_dump({"agents": agents, "fleet": fleet}))


def upstreams(seen):
    """Stand-ins for the sample, the agents and the fleet: echo what reached them."""
    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready", "version": "0.1.0"})
        if request.url.path == "/stream":
            async def events():
                for i in range(3):
                    yield f"data: {i}\n\n".encode()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=events())
        if request.url.path == "/redirect":
            return httpx.Response(307, headers={"location": "/console/"})
        return httpx.Response(200, headers=[("content-type", "application/json"), ("set-cookie", "a=1"), ("set-cookie", "b=2")],
                              json={"host": request.url.host, "path": request.url.path, "query": request.url.query.decode(),
                                    "prefix": request.headers.get("x-agentkit-prefix"),
                                    "platform": request.headers.get("x-agentkit-platform"),
                                    "user": request.headers.get("x-forwarded-email"),
                                    "cookie": request.headers.get("cookie"), "auth": request.headers.get("authorization")})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_router_mounts_each_agent_under_its_path_and_passes_the_signed_in_user(platform):
    write_registry(platform, [{"name": "legal", "internal": True}], fleet={"internal": True})
    seen = []
    with TestClient(platform.create_app(trusted_peer=None, client=upstreams(seen))) as http:
        legal = http.get("/agents/legal/console/agents?x=1", headers={**ADMIN, "x-agentkit-prefix": "/agents/evil",
                                                                     "cookie": "_oauth2_proxy=secret", "authorization": "Bearer x"}).json()
        moved = http.get("/agents/legal/redirect", headers=ADMIN, follow_redirects=False)
        not_platform = http.put("/v1/platform/agents", headers=ADMIN)
        newline = http.get("/agents/legal%0A/console", headers=ADMIN)
        sample = http.get("/console", headers={**ADMIN, "x-agentkit-prefix": "/agents/evil"})
        fleet = http.get("/fleet/v1/fleet/agents", headers=ADMIN).json()
        unknown = http.get("/agents/redis/", headers=ADMIN)
        stream = http.get("/agents/legal/stream", headers=ADMIN)
        post = http.post("/agents/legal/v1/agui", headers=ADMIN, content=b'{"x":1}')
        slash = http.get("/agents/legal", headers=ADMIN, follow_redirects=False)
    assert legal == {"host": "agent-legal", "path": "/console/agents", "query": "x=1", "prefix": "/agents/legal",
                     "platform": "1", "user": "ben@contoso.example", "cookie": None, "auth": None}  # nothing to replay
    assert moved.headers["location"] == "/agents/legal/console/"
    assert not_platform.status_code in (404, 405) and all(r.url.path != "/v1/platform/agents" for r in seen)
    assert newline.status_code == 404
    assert sample.json()["host"] == "agent" and sample.json()["prefix"] is None  # a client's prefix never gets through
    assert sample.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert fleet["host"] == "fleet" and fleet["prefix"] == "/fleet" and fleet["path"] == "/v1/fleet/agents"
    assert unknown.status_code == 404  # only registered agents: never an arbitrary container on the network
    assert stream.text == "data: 0\n\ndata: 1\n\ndata: 2\n\n"
    assert post.status_code == 200 and seen[-1].content == b'{"x":1}'
    assert slash.status_code == 307 and slash.headers["location"] == "/agents/legal/"


def test_only_the_sign_in_proxy_may_call_it(platform):
    write_registry(platform, [])
    with TestClient(platform.create_app(trusted_peer="auth.invalid", client=upstreams([]))) as http:
        assert http.get("/console", headers=ADMIN).status_code == 403
        assert http.get("/v1/platform", headers=ADMIN).status_code == 403
        assert http.get("/healthz").status_code == 200


class Recorder:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    async def __call__(self, job, argv, cwd, timeout=600):
        self.calls.append(argv)
        job.log.append("$ " + " ".join(argv))
        if self.fail_on and self.fail_on in argv:
            raise RuntimeError("docker compose up failed (exit 1)")
        if argv[0] == "copier":
            Path(argv[-1]).mkdir(parents=True)
        if argv[1:3] in (["agentctl.py", "add"], ["agentctl.py", "remove"]):  # what agentctl.py does to agents.yaml
            registry = cwd / "agents.yaml"
            data = yaml.safe_load(registry.read_text()) if registry.is_file() else {}
            data = data or {}
            agents = [a for a in data.get("agents") or [] if a["name"] != argv[3]]
            if argv[2] == "add":
                agents.append({"name": argv[3], "internal": True})
            registry.write_text(yaml.safe_dump({**data, "agents": agents}))


def wait_for(http, job_id, headers=ADMIN, until=("ready", "failed")):
    for _ in range(200):
        body = http.get(f"/v1/platform/jobs/{job_id}", headers=headers).json()
        if body["state"] in until:
            return body
        asyncio.run(asyncio.sleep(0.01))
    raise AssertionError("job didn't finish")


def test_create_agent_generates_registers_builds_and_starts_it(platform, monkeypatch):
    write_registry(platform, [], fleet={"internal": True})
    recorder = Recorder()
    builder = platform.Builder(runner=recorder)

    async def ready(job):
        return None

    monkeypatch.setattr(builder, "wait_ready", ready)
    with TestClient(platform.create_app(builder, trusted_peer=None, client=upstreams([]))) as http:
        info = http.get("/v1/platform", headers=ADMIN).json()
        assert info["is_admin"] and info["platform"] and info["fleet"]
        assert http.get("/v1/platform", headers=OTHER).json()["is_admin"] is False
        body = {"title": "Legal Desk", "description": "Answers contract questions.", "team": "legal"}
        assert http.post("/v1/platform/agents", headers=OTHER_W, json=body).status_code == 403
        assert http.post("/v1/platform/agents", headers={**ADMIN, "origin": "https://evil.example"}, json=body).status_code == 403
        assert http.post("/v1/platform/agents", headers=ADMIN, json=body).status_code == 403  # no Origin: not from the page
        assert http.post("/v1/platform/agents", headers=ADMIN_W, content=json.dumps(body)).status_code == 415
        assert http.post("/v1/platform/agents", headers=ADMIN_W, json=[body]).status_code == 422
        created = http.post("/v1/platform/agents", headers=ADMIN_W, json=body)
        assert created.status_code == 202 and created.json()["name"] == "legal-desk"
        done = wait_for(http, created.json()["id"])
    assert done["state"] == "ready" and done["created_by"] == "ben@contoso.example"
    copier, register, build, fleet = recorder.calls
    assert copier[:6] == ["copier", "copy", "--trust", "--defaults", "--vcs-ref", "HEAD"]
    assert "project_slug=legal-desk" in copier and "package_name=legal_desk" in copier  # any valid title generates
    assert "project_name=Legal Desk" in copier and "team=legal" in copier and "enable_teams=false" in copier
    assert copier[-2:] == [str(platform.HOME), str(platform.AGENTS_DIR / "legal-desk")]
    assert register[1:] == ["agentctl.py", "add", "legal-desk", str(platform.AGENTS_DIR / "legal-desk"), "--internal",
                            "--by", "ben@contoso.example"]
    assert build == ["docker", "compose", "up", "-d", "--build", "agent-legal-desk"]
    assert fleet == ["docker", "compose", "up", "-d", "fleet"]


@pytest.mark.parametrize("body", [{"title": "x"}, {"title": 'Legal "Desk"'}, {"title": "Legal Desk", "description": "a {{ b }}"},
                                  {"title": "Legal Desk", "description": "back\\slash"}, {"title": "Legal Desk", "team": "Bad Team"},
                                  {"title": "Legal Desk", "name": "redis"}, {"title": "Legal Desk", "name": "fleet"},
                                  {"title": "Legal Desk", "knowledge": "yes"}, {"title": "Legal Desk", "name": "../etc"},
                                  {"title": "Legal Desk", "name": "agent\n"}, {"title": "Legal Desk", "name": "le\ngal"},
                                  {"title": "Legal\nDesk"}, {"title": "Legal Desk", "team": "le\ngal"}])
def test_create_agent_refuses_input_that_could_escape_the_template(platform, body):
    write_registry(platform, [])
    with TestClient(platform.create_app(platform.Builder(runner=Recorder()), trusted_peer=None, client=upstreams([]))) as http:
        assert http.post("/v1/platform/agents", headers=ADMIN_W, json=body).status_code == 422


def test_a_failed_build_is_rolled_back_so_the_name_can_be_used_again(platform):
    write_registry(platform, [])
    recorder = Recorder(fail_on="--build")
    with TestClient(platform.create_app(platform.Builder(runner=recorder), trusted_peer=None, client=upstreams([]))) as http:
        created = http.post("/v1/platform/agents", headers=ADMIN_W, json={"title": "Legal Desk"}).json()
        done = wait_for(http, created["id"])
    assert done["state"] == "failed" and "exit 1" in done["error"] and done["failed_step"] == "building"
    assert ["docker", "compose", "rm", "-sf", "agent-legal-desk"] in recorder.calls
    assert any(c[1:4] == ["agentctl.py", "remove", "legal-desk"] for c in recorder.calls)
    assert not (platform.AGENTS_DIR / "legal-desk").exists()
    assert list((platform.AGENTS_DIR / ".failed").iterdir())  # kept for a look


def test_listing_and_removing(platform):
    write_registry(platform, [{"name": "legal", "internal": True, "service": "legal-desk", "title": "Legal Desk",
                               "created_by": "ben@contoso.example"},
                              {"name": "hr", "host": "hr.203-0-113-7.sslip.io", "port": 4183}])
    recorder = Recorder()
    (platform.AGENTS_DIR / "legal").mkdir()
    with TestClient(platform.create_app(platform.Builder(runner=recorder), trusted_peer=None, client=upstreams([]))) as http:
        rows = http.get("/v1/platform/agents", headers=OTHER).json()["agents"]
        assert [(r["name"], r["path"], r["status"]) for r in rows] == [
            ("sample", "", "ready"), ("legal", "/agents/legal", "ready"), ("hr", "/agents/hr", "ready")]
        assert "url" not in rows[1]  # internal addresses stay inside
        assert rows[1]["title"] == "Legal Desk"
        assert http.delete("/v1/platform/agents/legal", headers=OTHER_W).status_code == 403
        assert http.delete("/v1/platform/agents/legal", headers=ADMIN_W).status_code == 400  # needs the confirm header
        assert http.delete("/v1/platform/agents/hr", headers={**ADMIN_W, "x-agentkit-confirm": "hr"}).status_code == 409
        gone = http.delete("/v1/platform/agents/legal", headers={**ADMIN_W, "x-agentkit-confirm": "legal"})
        assert gone.status_code == 202
        done = wait_for(http, gone.json()["id"], until=("removed", "failed"))
        assert http.get(f"/v1/platform/jobs/{gone.json()['id']}", headers=OTHER).status_code == 404  # logs: admins
    assert done["state"] == "removed"
    assert ["docker", "compose", "rm", "-sf", "agent-legal"] in recorder.calls
    assert not (platform.AGENTS_DIR / "legal").exists() and list((platform.AGENTS_DIR / ".removed").iterdir())


def test_build_log_lines_are_plain_text(platform, tmp_path):
    job = platform.Job("x", "X", "ben@contoso.example")
    script = tmp_path / "colour.sh"
    script.write_text("printf '\\033[32m\\033[1m    create\\033[39m\\033[0m  src/app.py\\n'\n")
    asyncio.run(platform.Builder()._run(job, ["sh", str(script)], tmp_path))
    assert list(job.log)[-1] == "    create  src/app.py"


def test_rollback_tries_every_step_and_only_moves_what_it_made(platform):
    write_registry(platform, [])
    (platform.AGENTS_DIR / "taken").mkdir()  # someone else's folder with that name

    class Flaky(Recorder):
        async def __call__(self, job, argv, cwd, timeout=600):
            await super().__call__(job, argv, cwd, timeout)
            if argv[:3] == ["docker", "compose", "rm"]:
                raise RuntimeError("rm failed")

    recorder = Flaky(fail_on="--build")
    with TestClient(platform.create_app(platform.Builder(runner=recorder), trusted_peer=None, client=upstreams([]))) as http:
        taken = http.post("/v1/platform/agents", headers=ADMIN_W, json={"title": "Taken"})
        assert taken.status_code == 409 and (platform.AGENTS_DIR / "taken").is_dir()
        created = http.post("/v1/platform/agents", headers=ADMIN_W, json={"title": "Legal Desk"}).json()
        done = wait_for(http, created["id"])
    assert done["state"] == "failed"
    assert any(c[1:4] == ["agentctl.py", "remove", "legal-desk"] for c in recorder.calls)  # despite rm failing
    assert "legal-desk" not in (platform.VPS / "agents.yaml").read_text()


def test_a_fleet_restart_failure_doesnt_undo_a_healthy_agent(platform, monkeypatch):
    write_registry(platform, [], fleet={"internal": True})

    class FleetFails(Recorder):
        async def __call__(self, job, argv, cwd, timeout=600):
            await super().__call__(job, argv, cwd, timeout)
            if argv[-1] == "fleet":
                raise RuntimeError("fleet up failed")

    builder = platform.Builder(runner=FleetFails())

    async def ready(job):
        return None

    monkeypatch.setattr(builder, "wait_ready", ready)
    with TestClient(platform.create_app(builder, trusted_peer=None, client=upstreams([]))) as http:
        created = http.post("/v1/platform/agents", headers=ADMIN_W, json={"title": "HR Bot 2.0"}).json()
        done = wait_for(http, created["id"])
    assert done["state"] == "ready" and done["name"] == "hr-bot-2-0"
    assert any("fleet view didn't restart" in line for line in done["log"])
