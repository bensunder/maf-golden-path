"""Static checks on a rendered project (used for git mode, which can't be installed offline)."""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import yaml


def main(root: Path) -> int:
    errors: list[str] = []
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    deps = pyproject["project"]["dependencies"] + pyproject["project"]["optional-dependencies"]["dev"]
    for dep in deps:
        if "git+https://github.com/" not in dep or "#subdirectory=packages/agentkit-" not in dep:
            errors.append(f"unexpected dependency spec: {dep}")
    for path in root.rglob("*.py"):
        try:
            ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"syntax error in {path}: {exc}")
    for path in [*root.rglob("*.yml"), *root.rglob("*.yaml")]:
        yaml.safe_load(path.read_text())
    leftovers = [p for p in root.rglob("*") if p.is_file() and ("{{" in p.name or p.suffix == ".jinja")]
    errors += [f"unrendered file: {p}" for p in leftovers]
    import re

    jinja_leftover = re.compile(r"(?<!\$)\{\{|\{%")  # ${{ … }} is GitHub Actions syntax, not Jinja
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".bicep"}:
            if jinja_leftover.search(path.read_text()):
                errors.append(f"unrendered expression in {path}")
    if "git" not in (root / "Dockerfile").read_text():
        errors.append("git-mode Dockerfile must install git")
    for e in errors:
        print("FAIL", e)
    print("rendered project OK" if not errors else f"{len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
