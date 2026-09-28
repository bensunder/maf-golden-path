"""Template libraries for Create agent (agent-templates/, deploy/vps/platform/templates.py)."""

import asyncio
import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "deploy" / "vps" / "platform"
LIBRARIES = ROOT / "agent-templates"
CONTRACTS = "legal-agents/functional-domains/contract-lifecycle"
ADMIN = {"x-forwarded-email": "ben@contoso.example"}
ADMIN_W = {**ADMIN, "origin": "https://agent.203-0-113-7.sslip.io"}

sys.path.insert(0, str(PLATFORM))
import templates  # noqa: E402

BASE_SYSTEM = "# Role\nYou are Contracts Desk.\n\n# Tool use\n- Call `search_faq`.\n\n# Boundaries\n- Stay in scope.\n"
BASE_CHARTER = "## In scope\n- TODO: the questions and tasks this agent handles.\n\n## Out of scope\n- TODO\n"
BASE_CASES = "# Eval cases\ncases:\n  - id: faq-hours\n    input: When?\n    expect:\n      contains: [\"7am\"]\n"


def fake_agent(folder: Path, package: str) -> None:
    """What copier generates, as far as a template touches it."""
    (folder / "src" / package / "instructions").mkdir(parents=True)
    (folder / "src" / package / "instructions" / "system.md").write_text(BASE_SYSTEM)
    (folder / "evals").mkdir()
    (folder / "evals" / "cases.yaml").write_text(BASE_CASES)
    (folder / "agent.charter.md").write_text(BASE_CHARTER)


def test_the_legal_library_ships_thirty_templates_in_three_groups():
    catalog = templates.load_catalog(LIBRARIES)
    (legal,) = [lib for lib in catalog["libraries"] if lib["name"] == "legal-agents"]
    assert [g["title"] for g in legal["groups"]] == ["Practice areas", "Legal functions", "Jurisdictions and specialties"]
    assert sum(len(g["templates"]) for g in legal["groups"]) == 30
    assert legal["license"] == "MIT" and legal["source"] == "https://github.com/judicialmind/legal-agents"
    assert legal["commit"].startswith("20587b4") and (LIBRARIES / "legal-agents" / "LICENSE").is_file()
    contracts = catalog["templates"][CONTRACTS]
    assert contracts["name"] == "Contract Lifecycle Manager" and "Redlining and markup" in contracts["services"]
    assert "path" not in templates.public(contracts)
    assert "_meta" not in templates.library_view(legal) and legal["evals"] == ["legal-no-invented-citation", "legal-not-legal-advice"]


def test_applying_a_template_writes_instructions_scope_evals_and_the_license(tmp_path):
    catalog = templates.load_catalog(LIBRARIES)
    entry = catalog["templates"][CONTRACTS]
    library = next(lib for lib in catalog["libraries"] if lib["name"] == "legal-agents")
    fake_agent(tmp_path, "contracts_desk")
    changed = templates.apply_template(tmp_path, "contracts_desk", entry, library)
    assert changed[-1] == "evals/cases.yaml (+2 cases)"

    system = (tmp_path / "src" / "contracts_desk" / "instructions" / "system.md").read_text()
    assert system.startswith(BASE_SYSTEM.rstrip())  # the kit's own rules stay, first
    role, rules = system.index("# Role and procedure: Contract Lifecycle Manager"), system.index("# Rules that take precedence")
    assert role < system.index("You are **Contract Lifecycle Manager**") < rules
    assert "---" not in system.splitlines()[0:3] and "tools:\n" not in system  # no front matter
    assert "not a lawyer" in system[rules:] and "Never state a case name" in system[rules:]

    charter = (tmp_path / "agent.charter.md").read_text()
    assert "- Redlining and markup" in charter and "TODO: the questions" not in charter
    cases = yaml.safe_load((tmp_path / "evals" / "cases.yaml").read_text())["cases"]
    assert [c["id"] for c in cases] == ["faq-hours", "legal-no-invented-citation", "legal-not-legal-advice"]
    assert cases[1]["critical"] is True and " v. " in cases[1]["expect"]["not_contains"]
    notice = (tmp_path / "src" / "contracts_desk" / "instructions" / "TEMPLATE-LICENSE.txt").read_text()
    assert "judicialmind/legal-agents at 20587b4" in notice and "MIT License" in notice

    templates.apply_template(tmp_path, "contracts_desk", entry, library)  # again: no duplicate eval cases
    assert len(yaml.safe_load((tmp_path / "evals" / "cases.yaml").read_text())["cases"]) == 3


def test_only_real_named_templates_inside_the_library_are_offered(tmp_path):
    lib = tmp_path / "mine"
    (lib / "team").mkdir(parents=True)
    ok = "---\nname: Helper\nservices: [a, 3, b]\n---\n# Helper\nBody\n"
    (lib / "team" / "helper.md").write_text(ok)
    (lib / "README.md").write_text("# no front matter\n")
    (lib / "team" / "nameless.md").write_text("---\nemoji: x\n---\nBody\n")
    (lib / "team" / "Upper.md").write_text(ok)
    (lib / ".hidden").mkdir()
    (lib / ".hidden" / "h.md").write_text(ok)
    (lib / "team" / "big.md").write_text(ok + "x" * templates.MAX_FILE)
    (lib / "a" / "b" / "c").mkdir(parents=True)
    (lib / "a" / "b" / "c" / "deep.md").write_text(ok)  # more than three levels
    outside = tmp_path / "outside.md"
    outside.write_text(ok)
    os.symlink(outside, lib / "team" / "link.md")
    os.symlink(tmp_path / "mine" / "team", lib / "linked-dir")
    (tmp_path / "Bad_Name").mkdir()
    (tmp_path / "Bad_Name" / "x.md").write_text(ok)
    (lib / "library.yaml").write_text("source: javascript:alert(1)\ncommit: nothex\nrules: [not, text]\n")

    catalog = templates.load_catalog(tmp_path)
    assert list(catalog["templates"]) == ["mine/team/helper"]
    (mine,) = catalog["libraries"]
    assert mine["source"] == "" and mine["commit"] == "" and mine["rules"] == "" and mine["title"] == "mine"
    assert catalog["templates"]["mine/team/helper"]["services"] == ["a", "b"]
    assert templates.template_body(catalog["templates"]["mine/team/helper"]) == "Body"


@pytest.mark.parametrize("bad", ["legal-agents/../x", "legal-agents", "/etc/passwd", "legal-agents/a/b/c/d", "Legal/x",
                                 "legal-agents/x\n", "legal-agents//x"])
def test_template_ids_from_requests_are_strict(bad):
    assert not templates.TEMPLATE_ID.fullmatch(bad)


class Generator:
    """Stands in for the build's commands; copier "generates" a minimal agent."""

    def __init__(self):
        self.calls = []

    async def __call__(self, job, argv, cwd, timeout=600):
        self.calls.append(argv)
        if argv[0] == "copier":
            fake_agent(Path(argv[-1]), argv[-1].rsplit("/", 1)[-1].replace("-", "_"))
        if argv[1:3] == ["agentctl.py", "add"]:
            (cwd / "agents.yaml").write_text(yaml.safe_dump({"agents": [{"name": argv[3], "internal": True}]}))


@pytest.fixture
def platform(tmp_path, monkeypatch):
    for name in ("connectors", "platform_server"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("platform_server", PLATFORM / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = tmp_path / "kit"
    (home / "deploy" / "vps").mkdir(parents=True)
    shutil.copytree(LIBRARIES, home / "agent-templates")
    for attr, value in {"HOME": home, "VPS": home / "deploy" / "vps", "AGENTS_DIR": tmp_path / "agents",
                        "TEMPLATES_DIR": home / "agent-templates", "ADMINS": {"ben@contoso.example"},
                        "PUBLIC_HOST": "agent.203-0-113-7.sslip.io"}.items():
        monkeypatch.setattr(mod, attr, value)
    (tmp_path / "agents").mkdir()
    return mod


def test_create_agent_from_a_template(platform, monkeypatch):
    builder = platform.Builder(runner=Generator())

    async def ready(job, base=None):
        return None

    monkeypatch.setattr(builder, "wait_ready", ready)
    with TestClient(platform.create_app(builder, trusted_peer=None)) as http:
        assert http.get("/v1/platform/templates").status_code == 401
        libraries = http.get("/v1/platform/templates", headers=ADMIN).json()["libraries"]
        assert libraries[0]["name"] == "legal-agents" and "_folder" not in libraries[0]
        detail = http.get(f"/v1/platform/templates/{CONTRACTS}", headers=ADMIN).json()
        assert detail["body"].startswith("You are **Contract Lifecycle Manager**") and "not a lawyer" in detail["rules"]
        assert http.get("/v1/platform/templates/legal-agents/../../etc/passwd", headers=ADMIN).status_code == 404
        assert http.get("/v1/platform/templates/legal-agents/nope", headers=ADMIN).status_code == 404

        body = {"title": "Contracts Desk", "team": "legal"}
        for bad in ("legal-agents/nope", "../x", 7, "legal-agents/functional-domains/contract-lifecycle\n"):
            assert http.post("/v1/platform/agents", headers=ADMIN_W, json={**body, "template": bad}).status_code == 422
        created = http.post("/v1/platform/agents", headers=ADMIN_W, json={**body, "template": CONTRACTS})
        assert created.status_code == 202 and created.json()["template"] == CONTRACTS
        for _ in range(200):
            done = http.get(f"/v1/platform/jobs/{created.json()['id']}", headers=ADMIN).json()
            if done["state"] in ("ready", "failed"):
                break
            asyncio.run(asyncio.sleep(0.01))
    assert done["state"] == "ready", done
    assert any("apply template " + CONTRACTS in line for line in done["log"])
    system = (platform.AGENTS_DIR / "contracts-desk" / "src" / "contracts_desk" / "instructions" / "system.md").read_text()
    assert "# Role and procedure: Contract Lifecycle Manager" in system


def test_a_template_removed_during_the_build_fails_it_cleanly(platform):
    generator = Generator()

    async def remove_library_then_generate(job, argv, cwd, timeout=600):
        if argv[0] == "copier":
            shutil.rmtree(platform.TEMPLATES_DIR / "legal-agents")
        await generator(job, argv, cwd, timeout)

    with TestClient(platform.create_app(platform.Builder(runner=remove_library_then_generate), trusted_peer=None)) as http:
        created = http.post("/v1/platform/agents", headers=ADMIN_W, json={"title": "Contracts Desk", "template": CONTRACTS})
        for _ in range(200):
            done = http.get(f"/v1/platform/jobs/{created.json()['id']}", headers=ADMIN).json()
            if done["state"] in ("ready", "failed"):
                break
            asyncio.run(asyncio.sleep(0.01))
    assert done["state"] == "failed" and "no longer installed" in done["error"]
    assert not (platform.AGENTS_DIR / "contracts-desk").exists()  # rolled back: kept in .failed/
    assert not any(c[:2] == ["docker", "compose"] and "--build" in c for c in generator.calls)


def test_agentctl_lists_and_validates_template_libraries(capsys):
    spec = importlib.util.spec_from_file_location("agentctl_t", ROOT / "deploy" / "vps" / "agentctl.py")
    ctl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctl)
    ctl.main(["templates", "list"])
    out = capsys.readouterr().out
    assert "legal-agents" in out and "30 templates" in out and "MIT" in out
    for argv in (["templates", "add", "Bad_Name", "https://github.com/a/b"],
                 ["templates", "add", "mine", "file:///etc"], ["templates", "add", "mine", "ext::sh -c x"],
                 ["templates", "add", "mine", "https://github.com/a/b", "--ref=-x"],
                 ["templates", "remove", "legal-agents"], ["templates", "remove", "nope"]):
        with pytest.raises(SystemExit) as exc:
            ctl.main(argv)
        assert str(exc.value).startswith("agentctl:")
