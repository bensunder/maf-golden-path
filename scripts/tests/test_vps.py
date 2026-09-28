"""deploy/vps: the agent installer the image runs, and agentctl.py (more agents and the fleet on one host)."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

VPS = Path(__file__).resolve().parents[2] / "deploy" / "vps"


def load(name):
    spec = importlib.util.spec_from_file_location(name, VPS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_agent(root: Path, package="legal_desk", team="legal") -> Path:
    (root / "src" / package).mkdir(parents=True)
    (root / "src" / package / "app.py").write_text("app = None\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_evals.py").write_text("")
    (root / "pyproject.toml").write_text('[project]\nname = "legal-desk"\nversion = "0.1.0"\n'
                                         'dependencies = ["agentkit-hosting[redis] @ git+https://x/y@v1#subdirectory=p", "httpx>=0.27"]\n'
                                         '[project.optional-dependencies]\ndev = ["agentkit-testing>=0.9", "respx"]\n')
    (root / ".copier-answers.yml").write_text(f"team: {team}\n")
    return root


def test_installer_skips_the_kit_and_keeps_everything_else(tmp_path):
    install = load("install_agent")
    project = {"dependencies": ["agentkit-hosting[cosmos,redis] @ git+https://github.com/o/r@v0.9.1#subdirectory=x",
                                "agentkit_tools>=0.9", "psycopg[binary]>=3.2", "httpx", "agentkit-extras-by-someone-else"],
               "optional-dependencies": {"dev": ["agentkit-testing>=0.9,<0.10", "respx"]}}
    assert install.other_requirements(project) == ["psycopg[binary]>=3.2", "httpx", "agentkit-extras-by-someone-else", "respx"]
    assert install.KIT == {p.name for p in (VPS.parent.parent / "packages").iterdir() if p.is_dir()}
    assert install.app_target(make_agent(tmp_path / "a")) == "legal_desk.app:app"
    two = make_agent(tmp_path / "b")
    (two / "src" / "other").mkdir()
    (two / "src" / "other" / "app.py").write_text("")
    with pytest.raises(SystemExit):
        install.app_target(two)


def test_the_image_builds_whatever_agent_it_is_given():
    dockerfile = (VPS / "Dockerfile").read_text()
    ignore = (VPS / "Dockerfile.dockerignore").read_text().split()
    compose = yaml.safe_load((VPS / "docker-compose.yml").read_text())
    assert "COPY . ./agent" in dockerfile and "COPY --from=kit packages ./packages" in dockerfile
    # the agent's own .dockerignore leaves out tests/; this image's replaces it, and still keeps secrets out
    assert "tests" not in ignore and {".env", "**/.env", ".git", ".venv"} <= set(ignore)
    build = compose["services"]["agent"]["build"]
    root = VPS.parent.parent
    context = (VPS / build["context"]).resolve()
    assert context == root / "examples" / "order-status-agent"
    assert (context / build["dockerfile"]).resolve() == VPS / "Dockerfile"  # relative to the context
    kit = build["additional_contexts"]["kit"]
    assert (VPS / kit).resolve() == root and (context / kit).resolve() == root  # either way it's the checkout
    env = compose["services"]["agent"]["environment"]
    assert env["AGENTKIT_PRINCIPAL_CLAIMS_HEADER"] == "" and env["AGENTKIT_USER_HEADER"] == "x-forwarded-email"
    assert "ports" not in compose["services"]["agent"]  # only the sign-in proxy is published
    assert compose["services"]["auth"]["ports"] == ["127.0.0.1:${PROXY_PORT:-4180}:4180"]


@pytest.fixture
def stack(tmp_path):
    for name in ("agentctl.py", "docker-compose.yml"):
        shutil.copy(VPS / name, tmp_path / name)
    (tmp_path / ".env").write_text("AGENT_HOST=agent.203-0-113-7.sslip.io\nENTRA_CLIENT_ID=abc\n")
    make_agent(tmp_path / "legal")

    def run(*args, ok=True):
        result = subprocess.run([sys.executable, "agentctl.py", *args], cwd=tmp_path, capture_output=True, text=True)
        assert (result.returncode == 0) == ok, result.stderr + result.stdout
        return result.stdout + result.stderr

    return tmp_path, run


def test_agentctl_adds_agents_and_the_fleet_behind_their_own_sign_in(stack):
    root, run = stack
    out = run("add", "legal", str(root / "legal"))
    assert "certbot --nginx -d legal.203-0-113-7.sslip.io" in out
    assert "https://agent.203-0-113-7.sslip.io/oauth2/callback https://legal.203-0-113-7.sslip.io/oauth2/callback" in out
    run("fleet")
    run("add", "hr", str(root / "legal"), "--env", "AGENTKIT_MODEL=gpt-5-mini", "--env", "AGENTKIT_TEAM=people$ops")
    services = yaml.safe_load((root / "docker-compose.agents.yml").read_text())["services"]
    assert set(services) == {"agent-legal", "auth-legal", "fleet", "auth-fleet", "agent-hr", "auth-hr"}
    legal, auth = services["agent-legal"], services["auth-legal"]
    assert legal["build"]["context"] == str(root / "legal") and legal["build"]["dockerfile"] == str(root / "Dockerfile")
    assert legal["build"]["additional_contexts"]["kit"] == str(root.parent.parent)
    assert legal["environment"]["AGENTKIT_SERVICE_NAME"] == "legal-desk" and legal["environment"]["AGENTKIT_TEAM"] == "legal"
    assert legal["environment"]["AGENTKIT_SERVICE_VERSION"] == "0.1.0" and legal["environment"]["AGENTKIT_PRINCIPAL_CLAIMS_HEADER"] == ""
    assert legal["environment"]["AGENTKIT_USER_HEADER"] == "x-forwarded-email"  # same identity rules as the sample
    assert legal["environment"]["AGENTKIT_REDIS_URL"] == "redis://redis:6379/1"
    assert services["agent-hr"]["environment"]["AGENTKIT_REDIS_URL"] == "redis://redis:6379/2"
    assert services["agent-hr"]["environment"]["AGENTKIT_MODEL"] == "gpt-5-mini"
    assert services["agent-hr"]["environment"]["AGENTKIT_TEAM"] == "people$$ops"  # literal, not interpolated
    assert "ports" not in legal
    assert auth["ports"] == ["127.0.0.1:4181:4180"] and auth["environment"]["OAUTH2_PROXY_UPSTREAMS"] == "http://agent-legal:8000"
    assert auth["environment"]["OAUTH2_PROXY_REDIRECT_URL"] == "https://legal.203-0-113-7.sslip.io/oauth2/callback"
    fleet = services["fleet"]["environment"]
    assert fleet["AGENTKIT_REQUIRE_USER"] == "true" and fleet["AGENTKIT_FLEET_CALLER_HEADER"] == "x-forwarded-email: fleet@agentkit.local"
    assert fleet["AGENTKIT_FLEET_AGENTS"].split(";") == [
        "http://agent:8000||Order Status Agent|https://${AGENT_HOST}",
        "http://agent-legal:8000||legal|https://legal.203-0-113-7.sslip.io",
        "http://agent-hr:8000||hr|https://hr.203-0-113-7.sslip.io"]
    ports = [s["ports"][0] for n, s in services.items() if n.startswith("auth-")]
    assert len(set(ports)) == len(ports)
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.agents.yml" in (root / ".env").read_text()
    assert "proxy_pass http://127.0.0.1:4181;" in (root / "nginx" / "agentkit-legal.conf").read_text()
    assert oct((root / "agents.yaml").stat().st_mode & 0o777) == "0o600"
    assert oct((root / "docker-compose.agents.yml").stat().st_mode & 0o777) == "0o600"

    out = run("remove", "hr")
    assert "rm -f /etc/nginx/sites-enabled/agentkit-hr.conf" in out
    assert "agent-hr" not in yaml.safe_load((root / "docker-compose.agents.yml").read_text())["services"]
    assert not (root / "nginx" / "agentkit-hr.conf").exists()
    run("fleet", "--off")
    assert "fleet" not in yaml.safe_load((root / "docker-compose.agents.yml").read_text())["services"]


@pytest.mark.parametrize("args", [["add", "fleet", "legal"], ["add", "Bad_Name", "legal"], ["add", "docs", "."],
                                  ["add", "x2", "legal", "--host", "agent.203-0-113-7.sslip.io"],
                                  ["add", "x3", "legal", "--env", "lower=case"], ["remove", "nobody"],
                                  ["add", "x4", "legal", "--env", "AGENTKIT_CONSOLE_ROLE=Admin"],
                                  ["add", "x5", "legal", "--env", "AGENTKIT_USER_HEADER=x-user"]])
def test_agentctl_refuses_what_would_break_the_stack(stack, args):
    root, run = stack
    run(*[str(root / a) if a == "legal" else a for a in args], ok=False)
    assert not (root / "docker-compose.agents.yml").exists()


def test_agentctl_checks_a_hand_edited_list_and_keeps_other_compose_files(stack):
    root, run = stack
    (root / ".env").write_text((root / ".env").read_text() + "export COMPOSE_FILE=docker-compose.yml:extra.yml\n")
    run("add", "legal", str(root / "legal"))
    env = (root / ".env").read_text()
    assert "COMPOSE_FILE=docker-compose.yml:extra.yml:docker-compose.agents.yml" in env and env.count("COMPOSE_FILE") == 1
    good = (root / "agents.yaml").read_text()
    for bad in (good.replace("host: legal.203-0-113-7.sslip.io", "host: 'x; location /y {'"),
                good.replace("port: 4181", "port: '4181:80'"),
                good.replace("redis_db: 1", "redis_db: 40"),
                good.replace(str(root / "legal"), "legal; rm -rf /")):
        (root / "agents.yaml").write_text(bad)
        run("render", ok=False)
    (root / "agents.yaml").write_text(good)
    run("render")
