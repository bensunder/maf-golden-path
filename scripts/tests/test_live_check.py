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


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _serve(monkeypatch, routes):
    seen = []

    def get(url, headers=None, timeout=None):
        seen.append(url)
        for suffix, answer in routes.items():
            if url.endswith(suffix):
                return answer(len([u for u in seen if u.endswith(suffix)])) if callable(answer) else answer
        return _Resp(404, {})

    monkeypatch.setattr(live_check.httpx, "get", get)
    monkeypatch.setattr(live_check.time, "sleep", lambda s: None)
    return seen


def test_console_traffic_waits_for_runs(monkeypatch):
    def traffic(n):
        return _Resp(200, {"available": True, "totals": {"runs": 5 if n >= 2 else 0}})

    seen = _serve(monkeypatch, {"/v1/console/traffic?range=1h": traffic})
    checker = live_check.Checker("https://svc", {})
    checker.run_console_traffic(wait_seconds=600)
    assert len(seen) == 2 and all(c.ok for c in checker.checks) and len(checker.checks) == 2


def test_console_traffic_reports_why_it_is_not_connected(monkeypatch):
    _serve(monkeypatch, {"/v1/console/traffic?range=1h": _Resp(200, {"available": False, "error": "needs Monitoring Reader"})})
    checker = live_check.Checker("https://svc", {})
    checker.run_console_traffic(wait_seconds=0)
    assert not checker.checks[0].ok and "Monitoring Reader" in checker.checks[0].detail


def test_fleet_must_list_and_read_the_agent(monkeypatch):
    agents = {"agents": [{"url": "https://svc/", "status": "ready", "console": "ok", "source": "both"}],
              "discovery_error": None}
    _serve(monkeypatch, {"/v1/fleet/agents": _Resp(200, agents),
                         "/v1/fleet/traffic?range=1h": _Resp(200, {"available": True})})
    checker = live_check.Checker("https://svc", {})
    checker.run_fleet("https://fleet", agent_url="https://svc", discover=True)
    assert [c.name for c in checker.checks] == [
        "fleet lists the agent", "fleet sees the agent ready", "fleet reads the agent's console with its own identity",
        "fleet discovers the agent in Azure (tags + Easy Auth config)", "fleet traffic connects to Azure Monitor"]
    assert all(c.ok for c in checker.checks)


def test_fleet_denied_console_fails(monkeypatch):
    agents = {"agents": [{"url": "https://svc", "status": "ready", "console": "denied", "detail": "role"}]}
    _serve(monkeypatch, {"/v1/fleet/agents": _Resp(200, agents), "/v1/fleet/traffic?range=1h": _Resp(200, {})})
    checker = live_check.Checker("https://svc", {})
    checker.run_fleet("https://fleet", agent_url="https://svc", discover=False)
    failed = [c.name for c in checker.checks if not c.ok]
    assert failed == ["fleet reads the agent's console with its own identity", "fleet traffic connects to Azure Monitor"]


def test_fleet_waits_for_discovery_then_fails_if_it_never_finds_the_agent(monkeypatch):
    listed = {"agents": [{"url": "https://svc", "status": "ready", "console": "ok", "source": "registry"}]}
    found = {"agents": [{"url": "https://svc", "status": "ready", "console": "ok", "source": "both"}]}
    seen = _serve(monkeypatch, {"/v1/fleet/agents": lambda n: _Resp(200, found if n >= 3 else listed),
                                "/v1/fleet/traffic?range=1h": _Resp(200, {"available": True})})
    checker = live_check.Checker("https://svc", {})
    checker.run_fleet("https://fleet", agent_url="https://svc", discover=True, wait_seconds=600)
    assert sum(u.endswith("/v1/fleet/agents") for u in seen) == 3 and all(c.ok for c in checker.checks)

    _serve(monkeypatch, {"/v1/fleet/agents": _Resp(200, listed), "/v1/fleet/traffic?range=1h": _Resp(200, {"available": True})})
    checker = live_check.Checker("https://svc", {})
    checker.run_fleet("https://fleet", agent_url="https://svc", discover=True, wait_seconds=0)
    assert [c.name for c in checker.checks if not c.ok] == ["fleet discovers the agent in Azure (tags + Easy Auth config)"]
