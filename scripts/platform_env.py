"""Print the platform outputs a service needs, as `azd env set` or GitHub-variable commands.

    # after deploying infra/platform:
    python scripts/platform_env.py --resource-group rg-agentkit-dev                   # azd env set …
    python scripts/platform_env.py --resource-group rg-agentkit-dev --format github --repo org/svc --env dev
    python scripts/platform_env.py --outputs-file outputs.json                      # offline / CI

The deployment name defaults to "main" (az's default for main.bicep).
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys

KEYS = [
    "AGENTKIT_PLATFORM_RESOURCE_GROUP",
    "AGENTKIT_GATEWAY_ENDPOINT",
    "AGENTKIT_MODEL",
    "AGENTKIT_CONTENT_SAFETY_ENDPOINT",
    "AGENTKIT_CONTENT_SAFETY_NAME",
    "AGENTKIT_CONTAINER_APPS_ENVIRONMENT_ID",
    "AGENTKIT_REGISTRY_NAME",
    "AGENTKIT_REGISTRY_ENDPOINT",
    "AGENTKIT_APPINSIGHTS_NAME",
]


def load_outputs(args: argparse.Namespace) -> dict[str, str]:
    if args.outputs_file:
        raw = json.load(open(args.outputs_file, encoding="utf-8"))
    else:
        cmd = [
            "az", "deployment", "group", "show",
            "--resource-group", args.resource_group,
            "--name", args.deployment_name,
            "--query", "properties.outputs",
            "--output", "json",
        ]
        raw = json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)
    values = {}
    for key, entry in raw.items():
        values[key] = entry["value"] if isinstance(entry, dict) and "value" in entry else entry
    missing = [k for k in KEYS if k not in values]
    if missing:
        raise SystemExit(f"platform deployment is missing outputs: {missing}")
    return {k: str(values[k]) for k in KEYS}


def render(values: dict[str, str], fmt: str, repo: str | None, env: str | None) -> list[str]:
    lines = []
    for key, value in values.items():
        if fmt == "azd":
            lines.append(f"azd env set {key} {shlex.quote(value)}")
        elif fmt == "github":
            target = f" --repo {repo}" if repo else ""
            target += f" --env {env}" if env else ""
            lines.append(f"gh variable set {key} --body {shlex.quote(value)}{target}")
        else:
            lines.append(f"{key}={value}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--resource-group")
    source.add_argument("--outputs-file")
    parser.add_argument("--deployment-name", default="main")
    parser.add_argument("--format", choices=["azd", "github", "dotenv"], default="azd")
    parser.add_argument("--repo")
    parser.add_argument("--env")
    args = parser.parse_args(argv)
    print("\n".join(render(load_outputs(args), args.format, args.repo, args.env)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
