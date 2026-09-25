"""azd preprovision hook: fail fast with a clear message if platform values are missing.

Load them once with the kit's helper:
    python <kit>/scripts/platform_env.py --resource-group rg-agentkit-dev | sh
or set them one by one with `azd env set NAME value`.
"""

from __future__ import annotations

import os
import sys

REQUIRED = [
    "AGENTKIT_PLATFORM_RESOURCE_GROUP",
    "AGENTKIT_CONTAINER_APPS_ENVIRONMENT_ID",
    "AGENTKIT_REGISTRY_NAME",
    "AGENTKIT_CONTENT_SAFETY_NAME",
    "AGENTKIT_APPINSIGHTS_NAME",
    "AGENTKIT_GATEWAY_ENDPOINT",
    "AGENTKIT_MODEL",
]


def main() -> int:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    problems = [f"missing azd env value: {name}" for name in missing]
    if os.environ.get("AGENTKIT_ENVIRONMENT") == "prod" and not os.environ.get("AGENTKIT_AUTH_CLIENT_ID"):
        problems.append("prod requires AGENTKIT_AUTH_CLIENT_ID (Entra app registration for Easy Auth)")
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    if problems:
        print("See docs/deploy.md in the agentkit repo.", file=sys.stderr)
        return 1
    print("agentkit platform settings OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
