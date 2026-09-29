"""Quality-gate results into LangSmith: the eval cases as a dataset, each gate run as an experiment.

The gate stays the deciding check (it runs in CI and on every build). LangSmith is where people browse and
compare the results over time: each case becomes a dataset example (upserted by case id), each gate run an
experiment with one run per case and feedback for its pass rate, pass/fail, judge scores and failures.

    agentkit-gate --cases evals/cases.yaml --factory my_agent.agent:create_agent --live \\
        --report gate-report.json --langsmith-dataset my-agent-evals

    # or upload a report written earlier (the VPS build writes evals/gate-report.json):
    agentkit-langsmith --report evals/gate-report.json --cases evals/cases.yaml --dataset my-agent-evals

Needs ``LANGSMITH_API_KEY`` (and ``LANGSMITH_ENDPOINT`` for EU or self-hosted LangSmith) and the ``langsmith``
package (``pip install agentkit-testing[langsmith]``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from .evals import EvalCase, load_eval_cases

__all__ = ["main", "upload_report"]


def _client() -> Any:
    try:
        from langsmith import Client
    except ImportError as exc:  # pragma: no cover - optional extra
        raise SystemExit("the langsmith package isn't installed: pip install 'agentkit-testing[langsmith]'") from exc
    return Client()


def _dataset(client: Any, name: str, description: str) -> Any:
    if client.has_dataset(dataset_name=name):
        return client.read_dataset(dataset_name=name)
    return client.create_dataset(name, description=description)


def upload_report(
    report: dict[str, Any],
    cases: list[EvalCase],
    *,
    dataset: str,
    experiment: str | None = None,
    metadata: dict[str, Any] | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Upsert ``cases`` into ``dataset`` and record ``report`` as a new experiment. Returns what was written."""
    client = client or _client()
    ds = _dataset(client, dataset, "agentkit eval cases (evals/cases.yaml): upserted by case id")
    existing = {(e.metadata or {}).get("case_id"): e for e in client.list_examples(dataset_id=ds.id)}
    by_id = {c.id: c for c in cases}
    new = [c for c in cases if c.id not in existing]
    if new:
        client.create_examples(dataset_id=ds.id, examples=[
            {"inputs": {"input": c.input, **({"user": c.user} if c.user else {})},
             "outputs": {"expect": c.expect}, "metadata": {"case_id": c.id, "critical": c.critical}} for c in new])
        existing = {(e.metadata or {}).get("case_id"): e for e in client.list_examples(dataset_id=ds.id)}

    started = dt.datetime.fromtimestamp(float(report.get("started_at") or 0) or dt.datetime.now().timestamp(), dt.timezone.utc)
    name = experiment or f"{dataset} gate {started:%Y-%m-%d %H:%M:%S}"
    meta = {"mode": "live" if report.get("live") else "offline", "repeat": report.get("repeat"),
            "passed": report.get("passed"), "pass_rate": report.get("pass_rate"), **(metadata or {})}
    client.create_project(name, reference_dataset_id=ds.id, metadata=meta,
                          description="agentkit quality gate: " + ("passed" if report.get("passed") else "FAILED"))
    runs = []
    for stats in report.get("cases", []):
        case = by_id.get(stats["id"])
        example = existing.get(stats["id"])
        run_id = uuid.uuid4()
        end = started + dt.timedelta(seconds=float(stats.get("mean_duration_s") or 0))
        client.create_run(stats["id"], {"input": case.input if case else ""}, "chain", id=run_id, project_name=name,
                          reference_example_id=example.id if example else None, start_time=started, end_time=end,
                          outputs={"pass_rate": stats.get("pass_rate"), "runs": stats.get("runs"),
                                   "failures": stats.get("failures") or [], "skipped": stats.get("skipped") or []},
                          extra={"metadata": {"critical": stats.get("critical"), "mean_tokens": stats.get("mean_tokens")}})
        client.create_feedback(run_id, key="pass_rate", score=float(stats.get("pass_rate") or 0))
        client.create_feedback(run_id, key="passed", score=1 if stats.get("passed_runs") == stats.get("runs") else 0,
                               comment="; ".join(stats.get("failures") or []) or None)
        for metric, score in (stats.get("scores") or {}).items():
            client.create_feedback(run_id, key=f"judge_{metric}", score=float(score))
        runs.append(str(run_id))
    return {"dataset": dataset, "experiment": name, "examples_added": len(new), "runs": runs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload an agentkit quality-gate report to LangSmith.")
    parser.add_argument("--report", type=Path, required=True, help="a gate report (agentkit-gate --report, or the build's)")
    parser.add_argument("--cases", type=Path, required=True, help="evals/cases.yaml")
    parser.add_argument("--dataset", required=True, help="LangSmith dataset name, e.g. my-agent-evals")
    parser.add_argument("--experiment", help="experiment name (default: dataset + time of the run)")
    args = parser.parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    result = upload_report(report, load_eval_cases(args.cases), dataset=args.dataset, experiment=args.experiment)
    print(f"LangSmith: {len(result['runs'])} case results in experiment '{result['experiment']}' "
          f"(dataset '{result['dataset']}', {result['examples_added']} new examples)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
