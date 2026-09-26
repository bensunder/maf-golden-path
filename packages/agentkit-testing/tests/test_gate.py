import json
import sys
import textwrap

import pytest
from agent_framework import Agent, tool

from agentkit.testing import GROUNDEDNESS_RUBRIC, Judge, ScriptedChatClient, load_eval_cases, reply, run_case, tool_call
from agentkit.testing.gate import CaseStats, evaluate_gate, main, run_gate


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order."""
    return f"Order {order_id}: shipped, tracking 1Z999"


def judge_that(score_fn):
    """A judge whose score is computed from the material it is shown (so tests can assert on the prompt)."""
    seen = []

    def turn(messages):
        prompt = messages[-1].text
        seen.append(prompt)
        return json.dumps({"score": score_fn(prompt), "reason": "test"})

    class _Many(ScriptedChatClient):
        def _next_message(self, messages):  # infinite script
            self._script = [turn]
            return super()._next_message(messages)

    return Judge(_Many()), seen


def cases_from(tmp_path, text):
    path = tmp_path / "cases.yaml"
    path.write_text(textwrap.dedent(text))
    return load_eval_cases(path)


# --- judge ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"score": 5, "reason": "ok"}', 5),
        ('Sure! ```json\n{"score": 3, "reason": "meh"}\n```', 3),
        ("I think it is good", None),
        ('{"score": 9}', None),
        ('{"score": "high"}', None),
    ],
)
async def test_judge_parsing_never_defaults_to_pass(raw, expected):
    judge = Judge(ScriptedChatClient(script=[reply(raw)]))
    score = await judge.score("rubric", "be good", query="q", response="r")
    assert score.score == expected
    assert score.passed is (expected is not None and expected >= 4)


async def test_judge_failure_is_a_failed_score():
    judge = Judge(ScriptedChatClient(script=[]))  # raises on call
    score = await judge.score("rubric", "x", query="q", response="r")
    assert score.score is None and not score.passed and "judge call failed" in score.reason


# --- new deterministic checks -------------------------------------------------------------------------


async def test_tool_args_and_budgets(tmp_path):
    cases = cases_from(tmp_path, """
        cases:
          - id: good
            input: where is a1001
            script:
              - tool: lookup_order
                args: {order_id: A1001}
              - reply: shipped
            expect:
              tool_args: {lookup_order: {order_id: a1001}}
              max_tool_calls: 1
          - id: wrong-args
            input: where is a1001
            script:
              - tool: lookup_order
                args: {order_id: A9999}
              - tool: lookup_order
                args: {order_id: A9998}
              - reply: shipped
            expect:
              tool_args: {lookup_order: {order_id: A1001}}
              max_tool_calls: 1
    """)
    factory = lambda client: Agent(client, tools=[lookup_order])  # noqa: E731
    good = await run_case(factory, cases[0])
    bad = await run_case(factory, cases[1])
    assert good.passed, good.summary()
    assert any("no 'lookup_order' call with arguments" in f for f in bad.failures)
    assert any("2 tool calls > max_tool_calls 1" in f for f in bad.failures)
    assert good.tool_calls[0].arguments == {"order_id": "A1001"} and "tracking 1Z999" in good.tool_calls[0].result


async def test_token_budget(tmp_path):
    (case,) = cases_from(tmp_path, """
        cases:
          - id: chatty
            input: hi
            script: [{reply: hello}]
            expect: {max_total_tokens: 100}
    """)
    client_script = [reply("hello", input_tokens=90, output_tokens=40)]
    result = await run_case(lambda c: Agent(ScriptedChatClient(script=client_script)), case, live=True)
    assert any("130 tokens > max_total_tokens 100" in f for f in result.failures)


def test_unknown_expect_keys_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown expect keys"):
        cases_from(tmp_path, """
            cases:
              - id: typo
                input: hi
                expect: {contain: [x]}
        """)


# --- judged checks ------------------------------------------------------------------------------------------


async def test_rubric_and_groundedness_use_tool_results(tmp_path):
    (case,) = cases_from(tmp_path, """
        cases:
          - id: judged
            input: where is A1001?
            script:
              - tool: lookup_order
                args: {order_id: A1001}
              - reply: It shipped; tracking 1Z999.
            expect:
              rubric: Gives the tracking number.
              grounded: true
    """)
    judge, seen = judge_that(lambda prompt: 5 if "1Z999" in prompt else 1)
    result = await run_case(lambda c: Agent(c, tools=[lookup_order]), case, judge=judge)
    assert result.passed, result.summary()
    assert {s.name: s.score for s in result.scores} == {"rubric": 5, "grounded": 5}
    grounded_prompt = next(p for p in seen if "TOOL RESULTS" in p)
    assert GROUNDEDNESS_RUBRIC[:40] in grounded_prompt and "Order A1001: shipped" in grounded_prompt


async def test_judged_checks_are_skipped_without_a_judge(tmp_path):
    (case,) = cases_from(tmp_path, """
        cases:
          - id: judged
            input: hi
            script: [{reply: hello}]
            expect: {rubric: Be nice., grounded: true}
    """)
    result = await run_case(lambda c: Agent(c), case)
    assert result.passed and result.skipped == ["rubric (no judge)", "grounded (no judge)"]


async def test_low_judge_score_fails(tmp_path):
    (case,) = cases_from(tmp_path, """
        cases:
          - id: judged
            input: hi
            script: [{reply: I invented a delivery date of Tuesday.}]
            expect: {rubric: No invented dates., min_score: 4}
    """)
    judge, _ = judge_that(lambda prompt: 2)
    result = await run_case(lambda c: Agent(c), case, judge=judge)
    assert not result.passed and "rubric scored 2 < 4" in result.failures[0]


# --- gate ------------------------------------------------------------------------------------------------------


def flaky_factory(correct_every: int):
    """A 'live' agent that answers correctly on every Nth run, like a nondeterministic model."""
    counter = {"n": 0}

    def factory(client):
        counter["n"] += 1
        text = "shipped" if counter["n"] % correct_every == 0 else "no idea"
        return Agent(ScriptedChatClient(script=[reply(text)]))

    return factory


async def test_gate_repeats_and_computes_pass_rate(tmp_path):
    cases = cases_from(tmp_path, """
        cases:
          - id: status
            input: where?
            expect: {contains: [shipped]}
    """)
    report = await run_gate(flaky_factory(2), cases, live=True, judge=None, repeat=4, min_pass_rate=0.4)
    (stats,) = report.cases
    assert (stats.runs, stats.passed_runs) == (4, 2)
    assert report.passed  # 50% >= 40%
    assert "2/4" in report.to_markdown()
    strict = await run_gate(flaky_factory(2), cases, live=True, judge=None, repeat=4, min_pass_rate=0.9)
    assert not strict.passed and "mean pass rate 50% < required 90%" in strict.reasons[0]


async def test_critical_cases_must_always_pass(tmp_path):
    cases = cases_from(tmp_path, """
        cases:
          - id: must-block
            input: where?
            critical: true
            expect: {contains: [shipped]}
    """)
    report = await run_gate(flaky_factory(3), cases, live=True, judge=None, repeat=3, min_pass_rate=0.0)
    assert not report.passed and "critical case `must-block` failed 2/3 runs" in report.reasons[0]


def test_baseline_regressions():
    now = [CaseStats("a", False, 10, 7, scores={"rubric": 3.9}), CaseStats("b", False, 10, 10, scores={"rubric": 4.8}),
           CaseStats("new", False, 1, 1)]
    baseline = {"version": 1, "cases": {"a": {"pass_rate": 1.0, "scores": {"rubric": 4.6}},
                                        "b": {"pass_rate": 1.0, "scores": {"rubric": 4.9}}}}
    reasons = evaluate_gate(now, baseline=baseline, min_pass_rate=0.5, max_regression=0.15, score_tolerance=0.5)
    assert any("`a` pass rate regressed 100% → 70%" in r for r in reasons)
    assert any("`a` rubric score regressed 4.6 → 3.9" in r for r in reasons)
    assert not any("`b`" in r or "`new`" in r for r in reasons)  # within tolerance / not in baseline


def test_cli_offline_report_summary_and_baseline(tmp_path, monkeypatch):
    pkg = tmp_path / "demo_agent.py"
    pkg.write_text(textwrap.dedent("""
        from agent_framework import Agent
        def create_agent(client=None):
            return Agent(client)
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("demo_agent", None)
    cases = tmp_path / "cases.yaml"
    cases.write_text(textwrap.dedent("""
        cases:
          - id: hello
            input: hi
            script: [{reply: hello there}]
            expect: {contains: [hello], rubric: Friendly.}
          - id: wrong
            input: hi
            script: [{reply: go away}]
            expect: {contains: [hello]}
    """))
    report, summary, baseline = tmp_path / "r.json", tmp_path / "s.md", tmp_path / "baseline.json"
    code = main(["--cases", str(cases), "--factory", "demo_agent:create_agent", "--report", str(report),
                 "--summary", str(summary), "--min-pass-rate", "1.0", "--baseline", str(baseline), "--update-baseline"])
    assert code == 1  # one of two cases fails → 50% < 100%
    data = json.loads(report.read_text())
    assert data["passed"] is False and data["pass_rate"] == 0.5
    assert "rubric (no judge)" in data["cases"][0]["skipped"]
    assert "Agent quality gate: FAILED" in summary.read_text()
    assert json.loads(baseline.read_text())["cases"]["hello"]["pass_rate"] == 1.0
    # passes when the bar is 50%, now also comparing against the written baseline
    assert main(["--cases", str(cases), "--factory", "demo_agent:create_agent", "--min-pass-rate", "0.5",
                 "--baseline", str(baseline), "--summary", str(summary)]) == 0


# --- judge calibration and live-run helpers ------------------------------------------------------------


def _calibration_file(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text("""items:
  - id: good
    kind: rubric
    rubric: Gives the tracking number.
    query: Where is A1?
    response: Shipped, tracking 1Z9.
    human: pass
  - id: invented
    kind: grounded
    query: When will A1 arrive?
    context: 'lookup_order -> {"eta": "2026-09-26"}'
    response: Tomorrow before 9am.
    human: fail
  - id: vague
    kind: rubric
    rubric: Gives the tracking number.
    query: Where is A1?
    response: It's on its way.
    human: fail
""")
    return path


class _ScriptedJudge:
    """Returns fixed scores per response, and records what it was asked."""

    def __init__(self, scores):
        self.scores, self.asked = scores, []

    async def score(self, name, rubric, *, query, response, context=None, threshold=4):
        from agentkit.testing.judge import JudgeScore

        self.asked.append((name, context))
        return JudgeScore(name, self.scores[response], "scripted", threshold)


async def test_calibration_measures_agreement_and_false_passes(tmp_path):
    from agentkit.testing.calibration import load_calibration, run_calibration

    items = load_calibration(_calibration_file(tmp_path))
    agreeing = _ScriptedJudge({"Shipped, tracking 1Z9.": 5, "Tomorrow before 9am.": 2, "It's on its way.": 2})
    report = await run_calibration(agreeing, items)
    assert report.passed and report.agreement == 1.0 and report.kappa == 1.0
    assert agreeing.asked[1] == ("grounded", 'lookup_order -> {"eta": "2026-09-26"}')

    lenient = _ScriptedJudge({"Shipped, tracking 1Z9.": 5, "Tomorrow before 9am.": 5, "It's on its way.": 2})
    report = await run_calibration(lenient, items, min_agreement=0.5)
    assert report.false_passes == ["invented"] and not report.passed  # agreement ok, but a false pass
    assert "⚠️ false pass" in report.to_markdown()


def test_calibration_file_is_validated(tmp_path):
    import pytest as _pytest

    from agentkit.testing.calibration import load_calibration

    bad = tmp_path / "bad.yaml"
    bad.write_text("items:\n  - {id: x, kind: rubric, query: q, response: r, human: maybe, rubric: z}\n")
    with _pytest.raises(ValueError, match="human must be"):
        load_calibration(bad)


def test_calibrate_cli(tmp_path, monkeypatch):
    import argparse

    from agentkit.testing import gate

    judge = _ScriptedJudge({"Shipped, tracking 1Z9.": 5, "Tomorrow before 9am.": 1, "It's on its way.": 3})
    args = argparse.Namespace(calibrate=_calibration_file(tmp_path), judge_temperature="0", min_agreement=0.8,
                              max_false_pass=0, summary=tmp_path / "summary.md", report=tmp_path / "r.json")
    assert gate._calibrate(args, judge=judge) == 0
    assert "Judge calibration: PASSED" in (tmp_path / "summary.md").read_text()


async def test_eval_users_map_to_real_accounts(monkeypatch):
    from agentkit.telemetry import get_run_context
    from agentkit.testing.evals import _as_user

    monkeypatch.setenv("AGENTKIT_EVAL_USERS", '{"sam@contoso.example": "eval-support@contoso.com"}')
    with _as_user("sam@contoso.example", "c"):
        assert get_run_context().user_id == "eval-support@contoso.com"
    with _as_user("riley@contoso.example", "c"):
        assert get_run_context().user_id == "riley@contoso.example"


def test_skip_user_cases(tmp_path, capsys, monkeypatch):
    from agentkit.testing import gate

    (tmp_path / "plain_agent.py").write_text(
        "from agent_framework import Agent\n\ndef create_agent(client):\n    return Agent(client)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("plain_agent", None)

    cases = tmp_path / "cases.yaml"
    cases.write_text("cases:\n  - id: plain\n    input: hi\n    script: [{reply: hello}]\n"
                     "  - id: as-user\n    input: hi\n    user: sam\n    script: [{reply: hello}]\n")
    code = gate.main(["--cases", str(cases), "--factory", "plain_agent:create_agent", "--skip-user-cases",
                      "--summary", str(tmp_path / "s.md")])
    out = capsys.readouterr().out
    assert code == 0 and "skipping 1 case(s) that run as a user: as-user" in out and "`as-user`" not in out


def test_skip_named_cases_and_reject_typos(tmp_path, capsys, monkeypatch):
    from agentkit.testing import gate

    (tmp_path / "plain_agent2.py").write_text(
        "from agent_framework import Agent\n\ndef create_agent(client):\n    return Agent(client)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    cases = tmp_path / "cases.yaml"
    cases.write_text("cases:\n  - id: a\n    input: hi\n    script: [{reply: hello}]\n"
                     "  - id: needs-carrier\n    input: hi\n    script: [{reply: hello}]\n")
    base = ["--cases", str(cases), "--factory", "plain_agent2:create_agent", "--summary", str(tmp_path / "s.md")]
    assert gate.main([*base, "--skip", "needs-carrier"]) == 0
    assert "skipping 1 case(s) by request: needs-carrier" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        gate.main([*base, "--skip", "needs-carier"])  # a typo must not silently skip nothing
