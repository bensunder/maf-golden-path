"""Install an agent project into the VPS image (run by deploy/vps/Dockerfile).

The kit's packages are already installed from this checkout, so every agent in the stack runs the same
kit version: the agent's own ``agentkit-*`` requirements (a git tag or a package feed) are skipped, and
its other dependencies are installed as declared. Writes the uvicorn target (``<package>.app:app``) to
``/app/agent-app``.

    python install_agent.py /app/agent
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
KIT = {"agentkit-channels", "agentkit-guardrails", "agentkit-hosting", "agentkit-knowledge", "agentkit-langgraph",
       "agentkit-telemetry", "agentkit-testing", "agentkit-tools"}  # installed from this checkout before the agent


def requirement_name(requirement: str) -> str:
    match = _NAME.match(requirement)
    return match.group(1).lower().replace("_", "-") if match else ""


def other_requirements(project: dict) -> list[str]:
    """Everything the agent needs besides the kit (runtime and dev, since the image runs the evals)."""
    wanted = list(project.get("dependencies") or []) + list((project.get("optional-dependencies") or {}).get("dev") or [])
    return [r for r in wanted if requirement_name(r) not in KIT]


def app_target(root: Path) -> str:
    apps = sorted(p for p in (root / "src").glob("*/app.py") if p.parent.name.isidentifier())
    if len(apps) != 1:
        raise SystemExit(f"expected one src/<package>/app.py in {root}, found {[str(a) for a in apps] or 'none'}")
    return f"{apps[0].parent.name}.app:app"


def main(root: str) -> None:
    path = Path(root)
    project = tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if not (path / "tests" / "test_evals.py").is_file():
        raise SystemExit("tests/test_evals.py is missing: the image runs the agent's evals as its quality gate")
    target = app_target(path)
    extra = other_requirements(project)
    if extra:
        subprocess.run([sys.executable, "-m", "pip", "install", *extra], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", str(path)], check=True)
    Path(os.environ.get("AGENTKIT_APP_FILE", "/app/agent-app")).write_text(target + "\n", encoding="utf-8")
    print(f"installed {project['name']} ({target}); kit packages from this checkout")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/app/agent")
