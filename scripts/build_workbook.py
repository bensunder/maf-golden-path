"""Generate infra/platform/ops/workbook.json from ops/queries.json (the single source of truth).

    python scripts/build_workbook.py            # write it
    python scripts/build_workbook.py --check    # CI: fail if it's out of date

The live validation runs every query in queries.json against the real workspace, so the dashboard
and alerts that ship are the ones that were tested."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parents[1] / "infra" / "platform" / "ops"


def workbook(queries: dict) -> dict:
    items: list[dict] = [
        {"type": 1, "name": "intro", "content": {"json": (
            "## agentkit operations\nEvery agent on the platform: model spend by team (from the AI gateway), "
            "outcomes, refusals, latency, approvals and document search. Queries live in "
            "`infra/platform/ops/queries.json`; the live validation runs each one.")}},
        {"type": 9, "name": "params", "content": {
            "version": "KqlParameterItem/1.0", "style": "pills", "queryType": 0,
            "resourceType": "microsoft.operationalinsights/workspaces",
            "parameters": [{
                "id": "5c1f6f2e-0d7a-4c1e-9d8b-2f1f5b6f0a01", "version": "KqlParameterItem/1.0",
                "name": "TimeRange", "label": "Time range", "type": 4, "isRequired": True,
                "value": {"durationMs": 86400000},
                "typeSettings": {"allowCustom": True, "selectableValues": [
                    {"durationMs": 3600000}, {"durationMs": 86400000}, {"durationMs": 604800000},
                    {"durationMs": 2592000000}]},
            }]}},
    ]
    for panel in queries["panels"]:
        query = panel["kql"] + ("\n| render timechart" if panel["viz"] == "timechart" else "")
        items.append({"type": 3, "name": panel["id"], "customWidth": "100" if panel["viz"] == "table" else "50",
                      "content": {"version": "KqlItem/1.0", "query": query, "size": 0, "title": panel["title"],
                                  "timeContextFromParameter": "TimeRange", "queryType": 0,
                                  "resourceType": "microsoft.operationalinsights/workspaces",
                                  "visualization": panel["viz"]}})
    return {"version": "Notebook/1.0", "items": items, "isLocked": False,
            "$schema": "https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    queries = json.loads((OPS / "queries.json").read_text(encoding="utf-8"))
    ids = [q["id"] for q in queries["panels"] + queries["alerts"]]
    if len(ids) != len(set(ids)):
        print("duplicate query ids in queries.json", file=sys.stderr)
        return 1
    rendered = json.dumps(workbook(queries), indent=2) + "\n"
    target = OPS / "workbook.json"
    if args.check:
        if target.read_text(encoding="utf-8") != rendered:
            print("infra/platform/ops/workbook.json is out of date: run python scripts/build_workbook.py", file=sys.stderr)
            return 1
        print("workbook up to date")
        return 0
    target.write_text(rendered, encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
