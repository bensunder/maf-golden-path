"""live_check.py's telemetry half, with the Log Analytics call stubbed (the HTTP half runs in the smoke test)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_check  # noqa: E402

QUERIES = json.loads(live_check.QUERIES.read_text())
ALL_IDS = [q["id"] for q in QUERIES["panels"] + QUERIES["alerts"]]


def run(monkeypatch, answers, *, expect=True, wait=0):
    calls = []

    def fake(workspace, kql):
        qid = next(q["id"] for q in QUERIES["panels"] + QUERIES["alerts"] if q["kql"] == kql)
        calls.append(qid)
        return answers(qid, calls.count(qid))

    monkeypatch.setattr(live_check, "_log_query", fake)
    monkeypatch.setattr(live_check.time, "sleep", lambda s: None)
    checker = live_check.Checker("http://x", {})
    checker.run_queries("ws", expect_telemetry=expect, wait_seconds=wait)
    return checker, calls


def test_every_dashboard_and_alert_query_is_run(monkeypatch):
    checker, calls = run(monkeypatch, lambda qid, n: ([{"x": 1}], None))
    assert sorted(set(calls)) == sorted(ALL_IDS)
    assert all(c.ok for c in checker.checks) and len(checker.checks) == len(ALL_IDS)


def test_waits_for_telemetry_then_reports_it(monkeypatch):
    # runs_by_outcome shows up on the third poll, as ingestion lags
    answers = lambda qid, n: (([{"Runs": 3}] if n >= 3 else []) if qid == "runs_by_outcome" else ([{"t": 1}], None)[0], None)  # noqa: E731
    checker, calls = run(monkeypatch, answers, wait=600)
    assert calls.count("runs_by_outcome") == 3
    assert next(c for c in checker.checks if c.name == "query runs_by_outcome returns data").ok


def test_missing_telemetry_fails_after_the_wait(monkeypatch):
    checker, _ = run(monkeypatch, lambda qid, n: ([], None), wait=0)
    failed = {c.name for c in checker.checks if not c.ok}
    assert failed == {"query runs_by_outcome returns data", "query tokens_by_team returns data"}


def test_a_broken_query_is_a_hard_failure(monkeypatch):
    checker, _ = run(monkeypatch, lambda qid, n: ([], "SemanticError: 'Propertie' is not a column") if qid == "approvals"
                     else ([{"x": 1}], None))
    broken = next(c for c in checker.checks if c.name == "query approvals runs")
    assert not broken.ok and broken.hard and "SemanticError" in broken.detail
    assert checker.report(None) == 1
