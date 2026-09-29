"""Quality-gate results into LangSmith, and traces to LangSmith's OTel endpoint."""

import json
from types import SimpleNamespace

from agentkit.telemetry import langsmith_exporter
from agentkit.testing import gate
from agentkit.testing.evals import EvalCase
from agentkit.testing.langsmith import upload_report


class FakeLangSmith:
    def __init__(self, existing=()):
        self.examples = [SimpleNamespace(id=f"ex-{c}", metadata={"case_id": c}) for c in existing]
        self.created, self.projects, self.runs, self.feedback = [], [], [], []

    def has_dataset(self, dataset_name):
        return bool(self.examples)

    def read_dataset(self, dataset_name):
        return SimpleNamespace(id="ds-1", name=dataset_name)

    def create_dataset(self, name, description=None):
        return SimpleNamespace(id="ds-1", name=name)

    def list_examples(self, dataset_id):
        return list(self.examples)

    def create_examples(self, dataset_id, examples):
        self.created.extend(examples)
        self.examples += [SimpleNamespace(id=f"ex-{e['metadata']['case_id']}", metadata=e["metadata"]) for e in examples]

    def create_project(self, name, **kwargs):
        self.projects.append((name, kwargs))

    def create_run(self, name, inputs, run_type, **kwargs):
        self.runs.append((name, inputs, run_type, kwargs))

    def create_feedback(self, run_id, key, score=None, comment=None):
        self.feedback.append((str(run_id), key, score, comment))


CASES = [EvalCase(id="faq-hours", input="When is support open?", expect={"contains": ["7am"]}),
         EvalCase(id="refund-needs-approval", input="Refund A1", expect={"approval_required": True}, critical=True)]
REPORT = {"passed": False, "live": True, "repeat": 3, "pass_rate": 0.83, "started_at": 1_700_000_000,
          "cases": [{"id": "faq-hours", "critical": False, "runs": 3, "passed_runs": 3, "pass_rate": 1.0,
                     "scores": {"rubric": 4.7}, "failures": [], "skipped": [], "mean_tokens": 900, "mean_duration_s": 2.1},
                    {"id": "refund-needs-approval", "critical": True, "runs": 3, "passed_runs": 2, "pass_rate": 0.67,
                     "scores": {}, "failures": ["no approval requested"], "skipped": [], "mean_tokens": 700,
                     "mean_duration_s": 1.5}]}


def test_cases_become_a_dataset_and_the_run_an_experiment_with_feedback():
    ls = FakeLangSmith(existing=["faq-hours"])
    done = upload_report(REPORT, CASES, dataset="orders-evals", client=ls, metadata={"version": "1.4.0"})
    assert [e["metadata"]["case_id"] for e in ls.created] == ["refund-needs-approval"]  # upserted by case id
    ((name, project),) = ls.projects
    assert project["reference_dataset_id"] == "ds-1" and project["metadata"]["version"] == "1.4.0"
    assert project["metadata"]["mode"] == "live" and "FAILED" in project["description"]
    assert [r[0] for r in ls.runs] == ["faq-hours", "refund-needs-approval"]
    assert ls.runs[0][3]["reference_example_id"] == "ex-faq-hours" and ls.runs[0][3]["project_name"] == name
    keys = {(r, k): (s, c) for r, k, s, c in ls.feedback}
    first, second = done["runs"]
    assert keys[(first, "passed")][0] == 1 and keys[(first, "judge_rubric")][0] == 4.7
    assert keys[(second, "passed")] == (0, "no approval requested") and keys[(second, "pass_rate")][0] == 0.67


def test_the_gate_verdict_never_depends_on_langsmith(tmp_path, monkeypatch, capsys):
    cases = tmp_path / "cases.yaml"
    cases.write_text("cases:\n  - id: hi\n    input: hi\n    script:\n      - reply: hello\n    expect:\n      contains: [hello]\n")
    factory = tmp_path / "fake_factory_mod.py"
    factory.write_text("from agentkit.hosting import build_agent, AgentKitSettings\n"
                       "def create_agent(settings=None, client=None):\n"
                       "    return build_agent(name='x', instructions='x', settings=AgentKitSettings(environment='local', _env_file=None), client=client)\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    def unreachable(*a, **k):
        raise ConnectionError("LangSmith is down")

    monkeypatch.setattr("agentkit.testing.langsmith.upload_report", unreachable)
    code = gate.main(["--cases", str(cases), "--factory", "fake_factory_mod:create_agent", "--langsmith-dataset", "x-evals",
                      "--summary", str(tmp_path / "summary.md")])
    assert code == 0 and "couldn't record the results in LangSmith" in capsys.readouterr().err


def test_traces_go_to_langsmiths_otel_endpoint_with_the_key_and_project():
    import pytest

    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    exporter = langsmith_exporter("lsv2_test", project="legal-desk")
    assert exporter._endpoint == "https://api.smith.langchain.com/otel/v1/traces"
    assert exporter._headers["x-api-key"] == "lsv2_test" and exporter._headers["Langsmith-Project"] == "legal-desk"
    eu = langsmith_exporter("k", project="p", endpoint="https://eu.api.smith.langchain.com/otel/")
    assert eu._endpoint == "https://eu.api.smith.langchain.com/otel/v1/traces"


def test_settings_read_the_standard_langsmith_variables(monkeypatch):
    from agentkit.hosting import AgentKitSettings

    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_x")
    monkeypatch.setenv("LANGSMITH_PROJECT", "agents-prod")
    s = AgentKitSettings(_env_file=None)
    assert s.langsmith_api_key.get_secret_value() == "lsv2_x" and s.langsmith_project == "agents-prod"
    assert s.langsmith_endpoint == "https://api.smith.langchain.com/otel"
