"""Offline validation for infrastructure and pipelines (no Azure subscription needed).

    python scripts/check_infra.py                      # kit: platform Bicep + kit workflows
    python scripts/check_infra.py --service <dir>      # also a rendered service: Bicep, azure.yaml, workflows

Requires `bicep` and `actionlint` on PATH (CI installs both; see Makefile `tools`).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import xml.dom.minidom
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "scripts" / "schemas" / "azure.yaml.json"


def run(cmd: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    """Success = exit 0 and no diagnostics on stderr (bicep prints warnings there, output on stdout)."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    diagnostics = proc.stderr.strip()
    ok = proc.returncode == 0 and " Warning " not in diagnostics and " Error " not in diagnostics
    return ok, (proc.stdout if ok else diagnostics or proc.stdout).strip()


def check_bicep(path: Path) -> list[str]:
    problems = []
    ok, out = run(["bicep", "build", str(path), "--stdout"])
    if not ok:
        problems.append(f"bicep build {path}: {out[-2000:]}")
    for file in sorted(path.parent.rglob("*.bicep")):
        ok, out = run(["bicep", "lint", str(file)])
        if not ok:
            problems.append(f"bicep lint {file}: {out[-2000:]}")
    return problems


def check_workflows(files: list[Path]) -> list[str]:
    if not files:
        return []
    ok, out = run(["actionlint", *map(str, files)])
    return [] if ok else [f"actionlint: {out[-3000:]}"]


def check_azure_yaml(path: Path) -> list[str]:
    import jsonschema

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    problems = [f"azure.yaml: {'/'.join(map(str, e.path))}: {e.message}" for e in errors]
    # The azd schema treats `host` etc. as open strings; assert the agentkit contract explicitly.
    api = (document.get("services") or {}).get("api") or {}
    expected = {"host": "containerapp", "language": "python"}
    for key, value in expected.items():
        if api.get(key) != value:
            problems.append(f"azure.yaml: services.api.{key} must be '{value}' (got {api.get(key)!r})")
    if (document.get("infra") or {}).get("provider") != "bicep":
        problems.append("azure.yaml: infra.provider must be 'bicep'")
    return problems


def check_parameters(service: Path) -> list[str]:
    """Every Bicep param without a default must be supplied by main.parameters.json."""
    problems = []
    params_file = service / "infra" / "main.parameters.json"
    supplied = set(json.loads(params_file.read_text())["parameters"])
    proc = subprocess.run(
        ["bicep", "build", str(service / "infra" / "main.bicep"), "--stdout"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        return ["main.parameters.json not checked: main.bicep does not compile"]
    template = json.loads(proc.stdout)
    for name, spec in template["parameters"].items():
        if "defaultValue" not in spec and name not in supplied:
            problems.append(f"main.parameters.json does not supply required param '{name}'")
    for name in supplied - set(template["parameters"]):
        problems.append(f"main.parameters.json supplies unknown param '{name}'")
    return problems


def check_contract(service: Path | None) -> list[str]:
    """Platform outputs, platform_env.py and the service's parameters/hook must agree on names."""
    import re

    sys.path.insert(0, str(ROOT / "scripts"))
    from platform_env import KEYS

    proc = subprocess.run(
        ["bicep", "build", str(ROOT / "infra" / "platform" / "main.bicep"), "--stdout"], capture_output=True, text=True
    )
    outputs = set(json.loads(proc.stdout)["outputs"])
    problems = [f"platform_env.py expects output {k} that platform/main.bicep does not produce" for k in KEYS if k not in outputs]
    if service:
        params_text = (service / "infra" / "main.parameters.json").read_text()
        referenced = set(re.findall(r"\$\{(AGENTKIT_[A-Z_]+)", params_text))
        optional = {"AGENTKIT_ENVIRONMENT", "AGENTKIT_AUTH_CLIENT_ID", "AGENTKIT_APPROVER_ROLE",
                    "AGENTKIT_TEAMS_APPROVER_GROUP_ID", "AGENTKIT_TEAMS_APPROVALS_CHANNEL_ID",
                    "AGENTKIT_KNOWLEDGE_EMBEDDING_MODEL"}
        for name in sorted(referenced - optional - set(KEYS)):
            problems.append(f"main.parameters.json uses ${{{name}}}, which the platform does not output")
        hook = (service / "scripts" / "check_platform_env.py").read_text()
        for name in sorted(referenced - optional):
            if f'"{name}"' not in hook:
                problems.append(f"preprovision hook does not check {name}")
    return problems


def check_ops() -> list[str]:
    """Dashboard and alert queries: the workbook is generated from queries.json, and every alert query
    yields the ``Value`` column its rule aggregates. (KQL itself is checked by running it: live validation.)"""
    problems = []
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_workbook.py"), "--check"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        problems.append(proc.stderr.strip() or "workbook check failed")
    queries = json.loads((ROOT / "infra" / "platform" / "ops" / "queries.json").read_text(encoding="utf-8"))
    for alert in queries["alerts"]:
        missing = {"id", "title", "description", "severity", "window", "frequency", "operator", "threshold", "kql"} - set(alert)
        if missing:
            problems.append(f"alert {alert.get('id')}: missing {sorted(missing)}")
        if "Value" not in alert.get("kql", ""):
            problems.append(f"alert {alert.get('id')}: query must produce a Value column")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", type=Path, help="rendered service project to validate as well")
    args = parser.parse_args(argv)

    for tool in ("bicep", "actionlint"):
        if not shutil.which(tool):
            print(f"FAIL: {tool} not on PATH (run `make tools`)")
            return 1
    if not shutil.which("shellcheck"):
        # GitHub's Ubuntu runners have shellcheck, and actionlint uses it for `run:` scripts.
        print("WARNING: shellcheck not installed; `run:` scripts are not checked locally but will be in CI "
              "(apt install shellcheck / brew install shellcheck)")

    problems: list[str] = []
    problems += check_bicep(ROOT / "infra" / "platform" / "main.bicep")
    xml.dom.minidom.parse(str(ROOT / "infra" / "platform" / "policies" / "ai-gateway.xml"))
    problems += check_workflows(sorted((ROOT / ".github" / "workflows").glob("*.yml")))
    problems += check_ops()

    problems += check_contract(args.service)
    if args.service:
        svc = args.service
        problems += check_bicep(svc / "infra" / "main.bicep")
        problems += check_parameters(svc)
        problems += check_azure_yaml(svc / "azure.yaml")
        problems += check_workflows(sorted((svc / ".github" / "workflows").glob("*.yml")))

    for problem in problems:
        print("FAIL", problem)
    print("infra OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
