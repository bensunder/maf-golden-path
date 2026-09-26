"""Quality gate: run eval cases (repeatedly, live), score them, compare with a baseline, pass or fail.

    python -m agentkit.testing.gate --cases evals/cases.yaml --factory my_agent:create_agent \\
        --live --repeat 3 --baseline evals/baseline.json --report eval-report.json

Gate rules (any one fails the gate, exit code 1):
1. a ``critical: true`` case failed on any repetition;
2. the mean pass rate across cases is below ``--min-pass-rate``;
3. a case's pass rate dropped more than ``--max-regression`` below its baseline;
4. a case's mean judge score (rubric / grounded) dropped more than ``--score-tolerance`` below baseline.

``--update-baseline`` writes the current results as the new baseline (commit it deliberately).
A Markdown summary goes to ``$GITHUB_STEP_SUMMARY`` when set, so results show on the run page.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import os
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .evals import CaseResult, EvalCase, load_eval_cases, run_case
from .judge import Judge

__all__ = ["CaseStats", "GateReport", "evaluate_gate", "main", "run_gate"]

BASELINE_VERSION = 1


@dataclass
class CaseStats:
    id: str
    critical: bool
    runs: int
    passed_runs: int
    scores: dict[str, float] = field(default_factory=dict)  # mean judge score per metric
    failures: list[str] = field(default_factory=list)  # distinct failure messages (first few)
    skipped: list[str] = field(default_factory=list)
    mean_tokens: float = 0.0
    mean_duration_s: float = 0.0  # wall time per run (model, tools and judge)

    @property
    def pass_rate(self) -> float:
        return self.passed_runs / self.runs if self.runs else 0.0


@dataclass
class GateReport:
    cases: list[CaseStats]
    passed: bool
    reasons: list[str]
    live: bool
    repeat: int
    started_at: float
    duration_s: float
    baseline_used: bool

    @property
    def pass_rate(self) -> float:
        return statistics.fmean(c.pass_rate for c in self.cases) if self.cases else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": self.reasons,
            "live": self.live,
            "repeat": self.repeat,
            "pass_rate": round(self.pass_rate, 4),
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 2),
            "baseline_used": self.baseline_used,
            "cases": [{**asdict(c), "pass_rate": round(c.pass_rate, 4)} for c in self.cases],
        }

    def to_baseline(self) -> dict[str, Any]:
        return {
            "version": BASELINE_VERSION,
            "pass_rate": round(self.pass_rate, 4),
            "cases": {c.id: {"pass_rate": round(c.pass_rate, 4), "scores": {k: round(v, 3) for k, v in c.scores.items()}}
                      for c in self.cases},
        }

    def to_markdown(self) -> str:
        icon = "✅" if self.passed else "❌"
        lines = [
            f"## {icon} Agent quality gate: {'passed' if self.passed else 'FAILED'}",
            "",
            f"Mode: **{'live' if self.live else 'offline'}** · repetitions: **{self.repeat}** · "
            f"mean pass rate: **{self.pass_rate:.0%}** · baseline: **{'yes' if self.baseline_used else 'none'}** · "
            f"{self.duration_s:.0f}s",
            "",
        ]
        if self.reasons:
            lines += ["**Why it failed:**", ""] + [f"- {r}" for r in self.reasons] + [""]
        lines += ["| Case | Pass rate | Scores | Notes |", "|---|---|---|---|"]
        for c in self.cases:
            scores = ", ".join(f"{k} {v:.1f}" for k, v in c.scores.items()) or "–"
            notes = "; ".join(c.failures[:2] + c.skipped[:2]).replace("|", "\\|")[:200] or ""
            mark = " 🔒" if c.critical else ""
            lines.append(f"| `{c.id}`{mark} | {c.passed_runs}/{c.runs} | {scores} | {notes} |")
        return "\n".join(lines) + "\n"


def _stats(case: EvalCase, results: list[CaseResult]) -> CaseStats:
    failures: list[str] = []
    for r in results:
        for f in r.failures:
            if f not in failures:
                failures.append(f)
    by_metric: dict[str, list[float]] = {}
    for r in results:
        for s in r.scores:
            by_metric.setdefault(s.name, []).append(float(s.score) if s.score is not None else 0.0)
    return CaseStats(
        id=case.id,
        critical=case.critical,
        runs=len(results),
        passed_runs=sum(r.passed for r in results),
        scores={k: statistics.fmean(v) for k, v in by_metric.items()},
        failures=failures[:5],
        skipped=sorted({s for r in results for s in r.skipped}),
        mean_tokens=statistics.fmean(r.total_tokens for r in results) if results else 0.0,
    )


def evaluate_gate(
    stats: list[CaseStats],
    *,
    baseline: dict[str, Any] | None,
    min_pass_rate: float,
    max_regression: float,
    score_tolerance: float,
) -> list[str]:
    """Return the reasons the gate fails (empty = pass)."""
    reasons: list[str] = []
    for c in stats:
        if c.critical and c.pass_rate < 1.0:
            reasons.append(f"critical case `{c.id}` failed {c.runs - c.passed_runs}/{c.runs} runs: "
                           f"{c.failures[0] if c.failures else ''}")
    overall = statistics.fmean(c.pass_rate for c in stats) if stats else 0.0
    if overall < min_pass_rate:
        reasons.append(f"mean pass rate {overall:.0%} < required {min_pass_rate:.0%}")
    if baseline:
        base_cases = baseline.get("cases", {})
        for c in stats:
            base = base_cases.get(c.id)
            if not base:
                continue
            if c.pass_rate < base["pass_rate"] - max_regression - 1e-9:
                reasons.append(f"`{c.id}` pass rate regressed {base['pass_rate']:.0%} → {c.pass_rate:.0%}")
            for metric, value in c.scores.items():
                before = base.get("scores", {}).get(metric)
                if before is not None and value < before - score_tolerance - 1e-9:
                    reasons.append(f"`{c.id}` {metric} score regressed {before:.1f} → {value:.1f}")
    return reasons


async def run_gate(
    agent_factory: Callable[[Any], Any],
    cases: list[EvalCase],
    *,
    live: bool,
    judge: Judge | None,
    repeat: int,
    baseline: dict[str, Any] | None = None,
    min_pass_rate: float = 0.9,
    max_regression: float = 0.15,
    score_tolerance: float = 0.5,
) -> GateReport:
    started, t0 = time.time(), time.perf_counter()
    stats = []
    for case in cases:
        n = case.repeat or repeat
        results, durations = [], []
        for _ in range(n):
            t = time.perf_counter()
            results.append(await run_case(agent_factory, case, live=live, judge=judge))
            durations.append(time.perf_counter() - t)
        case_stats = _stats(case, results)
        case_stats.mean_duration_s = round(statistics.fmean(durations), 3) if durations else 0.0
        stats.append(case_stats)
    reasons = evaluate_gate(stats, baseline=baseline, min_pass_rate=min_pass_rate,
                            max_regression=max_regression, score_tolerance=score_tolerance)
    return GateReport(stats, not reasons, reasons, live, repeat, started, time.perf_counter() - t0, bool(baseline))


# ------------------------------------------------------------------------------------------ CLI
def _load_factory(spec: str, live: bool) -> Callable[[Any], Any]:
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit(f"--factory must look like 'package.module:create_agent', got {spec!r}")
    create = getattr(importlib.import_module(module_name), attr)
    params = inspect.signature(create).parameters
    if "settings" not in params:
        return lambda client: create(client=client) if "client" in params else create(client)

    from agentkit.hosting import AgentKitSettings

    settings = AgentKitSettings() if live else AgentKitSettings(environment="test", guardrail_mode="heuristic",
                                                                _env_file=None)
    return lambda client: create(settings=settings, client=client)


def _live_judge(temperature: str) -> Judge:
    from agentkit.hosting import AgentKitSettings, create_chat_client

    settings = AgentKitSettings()
    judge_model = os.getenv("AGENTKIT_JUDGE_MODEL")
    if judge_model:
        settings = settings.model_copy(update={"model": judge_model})
    options = {} if temperature == "none" else {"temperature": float(temperature)}
    return Judge(create_chat_client(settings, agent_name="eval-judge"), model_options=options)


def _calibrate(args: argparse.Namespace, judge: Judge | None = None) -> int:
    from .calibration import load_calibration, run_calibration

    items = load_calibration(args.calibrate)
    judge = judge or _live_judge(args.judge_temperature)
    report = asyncio.run(run_calibration(judge, items, min_agreement=args.min_agreement,
                                         max_false_pass=args.max_false_pass))
    markdown = report.to_markdown()
    print(markdown)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(markdown)
    if args.report:
        args.report.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return 0 if report.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agentkit.testing.gate", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--factory", help="module:create_agent")
    parser.add_argument("--skip-user-cases", action="store_true",
                        help="skip cases that run as a user (live runs without real test accounts in AGENTKIT_EVAL_USERS)")
    parser.add_argument("--skip", action="append", default=[], metavar="CASE_ID",
                        help="skip a case by id (e.g. one that needs a system the environment doesn't have); repeatable")
    parser.add_argument("--calibrate", type=Path, metavar="LABELS.yaml",
                        help="instead of running cases: score the judge against human-graded answers")
    parser.add_argument("--min-agreement", type=float, default=0.8, help="calibration: minimum judge/human agreement")
    parser.add_argument("--max-false-pass", type=int, default=0,
                        help="calibration: answers the judge may pass that a human failed")
    parser.add_argument("--live", action="store_true", help="use the real model (gateway settings from env)")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--no-judge", action="store_true", help="skip rubric/grounded checks even when live")
    parser.add_argument("--judge-temperature", default="0", help="'none' for models that reject temperature")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--min-pass-rate", type=float, default=0.9)
    parser.add_argument("--max-regression", type=float, default=0.15)
    parser.add_argument("--score-tolerance", type=float, default=0.5)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--summary", type=Path, default=os.getenv("GITHUB_STEP_SUMMARY") or None)
    args = parser.parse_args(argv)
    if args.calibrate:
        return _calibrate(args)
    if not args.cases or not args.factory:
        parser.error("--cases and --factory are required (or use --calibrate)")

    cases = load_eval_cases(args.cases)
    unknown = set(args.skip) - {c.id for c in cases}
    if unknown:
        parser.error(f"--skip: no such case(s): {sorted(unknown)}")
    if args.skip:
        cases = [c for c in cases if c.id not in set(args.skip)]
        print(f"skipping {len(args.skip)} case(s) by request: {', '.join(args.skip)}")
    if args.skip_user_cases:
        skipped = [c.id for c in cases if c.user]
        cases = [c for c in cases if not c.user]
        if skipped:
            print(f"skipping {len(skipped)} case(s) that run as a user: {', '.join(skipped)}")
    factory = _load_factory(args.factory, args.live)
    judge = _live_judge(args.judge_temperature) if args.live and not args.no_judge else None
    baseline = None
    if args.baseline and args.baseline.exists() and not args.update_baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        if baseline.get("version") != BASELINE_VERSION:
            raise SystemExit(f"baseline {args.baseline} has version {baseline.get('version')}, expected {BASELINE_VERSION}")

    report = asyncio.run(run_gate(factory, cases, live=args.live, judge=judge, repeat=args.repeat, baseline=baseline,
                                  min_pass_rate=args.min_pass_rate, max_regression=args.max_regression,
                                  score_tolerance=args.score_tolerance))

    markdown = report.to_markdown()
    print(markdown)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(markdown)
    if args.report:
        args.report.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    if args.update_baseline:
        if not args.baseline:
            raise SystemExit("--update-baseline needs --baseline PATH")
        args.baseline.write_text(json.dumps(report.to_baseline(), indent=2) + "\n", encoding="utf-8")
        print(f"baseline written to {args.baseline} (review and commit it)")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
