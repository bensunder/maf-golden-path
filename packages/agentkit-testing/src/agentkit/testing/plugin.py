"""pytest plugin (auto-registered via the ``pytest11`` entry point)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from .evals import RESULT_SINKS

from .scripted_client import ScriptedChatClient
from .spans import SpanRecorder, install_span_recorder


@pytest.fixture
def scripted_client() -> ScriptedChatClient:
    """An empty scripted client; call ``.enqueue(...)`` to add model turns."""
    return ScriptedChatClient()


@pytest.fixture
def span_recorder() -> SpanRecorder:
    """Spans emitted during the test (cleared before each test)."""
    recorder = install_span_recorder()
    recorder.clear()
    return recorder


# --------------------------------------------------------------------------- eval report
def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--agentkit-eval-report",
        metavar="PATH",
        default=None,
        help="write a quality-gate report (agentkit-gate --report format) from the eval cases run in this session",
    )


class _EvalReportCollector:
    def __init__(self, path: str) -> None:
        self.path = path
        self.started = time.time()
        self.t0 = time.perf_counter()
        self.results: dict[str, tuple[Any, list[Any], list[float]]] = {}
        self.live: set[bool] = set()

    def __call__(self, case: Any, result: Any, seconds: float, live: bool = False) -> None:
        _, results, durations = self.results.setdefault(case.id, (case, [], []))
        results.append(result)
        durations.append(seconds)
        self.live.add(live)

    def write(self) -> None:
        from .gate import GateReport, _stats, evaluate_gate

        stats = []
        for case, results, durations in self.results.values():
            s = _stats(case, results)
            s.mean_duration_s = round(sum(durations) / len(durations), 3) if durations else 0.0
            stats.append(s)
        live = self.live == {True}
        repeat = max((s.runs for s in stats), default=1)
        # Offline, every scripted case must pass: the wiring either works or it doesn't. Live runs vary, so
        # they get the gate's default bar.
        reasons = evaluate_gate(stats, baseline=None, min_pass_rate=0.9 if live else 1.0, max_regression=0.0,
                                score_tolerance=0.0)
        report = GateReport(stats, not reasons, reasons, live, repeat, self.started, time.perf_counter() - self.t0, False)
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("--agentkit-eval-report", default=None)
    if path and hasattr(config, "workerinput"):  # pytest-xdist worker: results are split across processes
        return
    if path:
        if hasattr(config.option, "numprocesses") and config.option.numprocesses:
            raise pytest.UsageError("--agentkit-eval-report doesn't support pytest-xdist (-n); run the evals in one process")
        Path(path).unlink(missing_ok=True)  # never leave a previous run's report behind
        collector = _EvalReportCollector(path)
        config._agentkit_eval_report = collector  # type: ignore[attr-defined]
        RESULT_SINKS.append(collector)


def pytest_sessionfinish(session: pytest.Session) -> None:
    collector = getattr(session.config, "_agentkit_eval_report", None)
    if collector is not None:
        RESULT_SINKS.remove(collector)
        if collector.results:
            collector.write()
