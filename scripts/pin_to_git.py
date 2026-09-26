"""Point a feed-mode service at the kit in git (a repo + commit) instead of a package feed.

    python scripts/pin_to_git.py examples/order-status-agent --repo bensunder/maf-golden-path --ref <sha>

Live validation uses it to deploy the sample with exactly the kit code under test. It rewrites the
``agentkit-*`` requirements in pyproject.toml to ``git+https`` direct references (every agentkit package the
service needs, including transitive ones, so pip never looks for them on an index) and makes the
Dockerfile install git. Nothing else changes."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# agentkit package -> agentkit packages it depends on
KIT_DEPS = {
    "agentkit-testing": [],
    "agentkit-telemetry": [],
    "agentkit-guardrails": ["agentkit-telemetry"],
    "agentkit-tools": ["agentkit-telemetry"],
    "agentkit-hosting": ["agentkit-telemetry", "agentkit-guardrails"],
    "agentkit-channels": ["agentkit-hosting", "agentkit-tools"],
    "agentkit-knowledge": ["agentkit-hosting", "agentkit-tools"],
}
_REQ = re.compile(r'^(\s*)"(agentkit-[a-z]+)(\[[^\]]*\])?[^"]*",\s*$')


def _closure(names: set[str]) -> set[str]:
    out, todo = set(), list(names)
    while todo:
        name = todo.pop()
        if name not in out:
            out.add(name)
            todo.extend(KIT_DEPS.get(name, []))
    return out


def pin(service: Path, repo: str, ref: str) -> None:
    pyproject = service / "pyproject.toml"
    lines = pyproject.read_text(encoding="utf-8").splitlines()
    url = f"git+https://github.com/{repo}@{ref}#subdirectory=packages/{{name}}"
    runtime = {m.group(2) for line in lines if (m := _REQ.match(line)) and m.group(2) != "agentkit-testing"}
    missing = sorted(_closure(runtime) - runtime)
    out, added = [], False
    for line in lines:
        match = _REQ.match(line)
        if match:
            indent, name, extras = match.group(1), match.group(2), match.group(3) or ""
            out.append(f'{indent}"{name}{extras} @ {url.format(name=name)}",')
            if not added and name != "agentkit-testing":
                out += [f'{indent}"{m} @ {url.format(name=m)}",' for m in missing]
                added = True
        else:
            out.append(line)
    text = "\n".join(out) + "\n"
    if "allow-direct-references" not in text:
        text += "\n[tool.hatch.metadata]\nallow-direct-references = true\n"
    pyproject.write_text(text, encoding="utf-8")

    dockerfile = service / "Dockerfile"
    docker = dockerfile.read_text(encoding="utf-8")
    if "install -y --no-install-recommends git" not in docker:
        docker = re.sub(r"(ENV PYTHONDONTWRITEBYTECODE[^\n]*\n)",
                        r"\1RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*\n",
                        docker, count=1)
        dockerfile.write_text(docker, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service", type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--ref", required=True)
    args = parser.parse_args(argv)
    pin(args.service, args.repo, args.ref)
    print(f"{args.service}: agentkit pinned to {args.repo}@{args.ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
