"""pytest --agentkit-eval-report: a gate report written from the offline eval run."""

import json

pytest_plugins = ["pytester"]

TEST_FILE = '''
import pytest
from agent_framework import Agent, tool
from agentkit.testing import EvalCase, run_case


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order."""
    return f"Order {order_id}: shipped"


CASES = [
    EvalCase(id="shipped", input="Where is A1?", critical=True,
             script=[{"tool": "lookup_order", "args": {"order_id": "A1"}}, {"reply": "A1 has shipped."}],
             expect={"contains": ["shipped"], "tools": ["lookup_order"]}),
    EvalCase(id="wrong", input="Where is A2?", script=[{"reply": "No idea."}], expect={"contains": ["shipped"]}),
]


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_case(case):
    result = await run_case(lambda client: Agent(client, tools=[lookup_order]), case)
    assert result.passed or case.id == "wrong"
'''


def test_report_from_the_offline_run(pytester):
    pytester.makepyfile(test_evals=TEST_FILE)
    pytester.makeini("[pytest]\nasyncio_mode = auto\n")
    report = pytester.path / "evals" / "gate-report.json"
    result = pytester.runpytest("-p", "no:cacheprovider", f"--agentkit-eval-report={report}")
    result.assert_outcomes(passed=2)
    data = json.loads(report.read_text())
    cases = {c["id"]: c for c in data["cases"]}
    assert data["live"] is False and data["repeat"] == 1
    assert cases["shipped"]["passed_runs"] == 1 and cases["shipped"]["mean_duration_s"] >= 0
    assert cases["wrong"]["passed_runs"] == 0
    assert data["passed"] is False  # offline, every case must pass
    assert any("mean pass rate" in r for r in data["reasons"])


def test_a_run_without_eval_cases_leaves_no_stale_report(pytester):
    report = pytester.path / "gate-report.json"
    report.write_text('{"passed": true}')
    pytester.makepyfile(test_nothing="def test_nothing():\n    assert True\n")
    pytester.runpytest("-p", "no:cacheprovider", f"--agentkit-eval-report={report}").assert_outcomes(passed=1)
    assert not report.exists()
