"""Judge calibration: how often does the LLM judge agree with a human?

    agentkit-gate --calibrate evals/judge_calibration.yaml          # live judge from the gateway

A calibration set is a few dozen answers a person has already graded:

    items:
      - id: tracking-good
        kind: rubric                 # or: grounded
        rubric: Gives the status, carrier and exact tracking number.
        query: Where is A1001?
        response: Order A1001 has shipped with UPS, tracking 1Z999AA10123456784.
        human: pass                  # or: fail
      - id: invented-eta
        kind: grounded
        query: When will A1001 arrive?
        context: 'lookup_order -> {"status": "shipped", "eta": "2026-09-26"}'   # quote: it contains ": "
        response: It arrives tomorrow morning before 9am.
        human: fail

The report gives agreement, Cohen's kappa (agreement beyond chance), and splits disagreements into
**false passes** (the judge passed an answer a human failed: the dangerous kind, it lets regressions
through the gate) and false fails (noise). The gate fails when agreement is below ``--min-agreement``
or false passes exceed ``--max-false-pass``, so a judge model change can't silently weaken the gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .judge import GROUNDEDNESS_RUBRIC, Judge, JudgeScore

__all__ = ["CalibrationItem", "CalibrationReport", "load_calibration", "run_calibration"]


@dataclass
class CalibrationItem:
    id: str
    kind: str
    query: str
    response: str
    human_pass: bool
    rubric: str | None = None
    context: str | None = None
    threshold: int = 4


@dataclass
class CalibrationReport:
    items: list[tuple[CalibrationItem, JudgeScore]] = field(default_factory=list)
    min_agreement: float = 0.8
    max_false_pass: int = 0

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def agreements(self) -> int:
        return sum(1 for item, score in self.items if score.passed == item.human_pass)

    @property
    def agreement(self) -> float:
        return self.agreements / self.total if self.total else 0.0

    @property
    def false_passes(self) -> list[str]:
        return [item.id for item, score in self.items if score.passed and not item.human_pass]

    @property
    def false_fails(self) -> list[str]:
        return [item.id for item, score in self.items if not score.passed and item.human_pass]

    @property
    def kappa(self) -> float:
        """Cohen's kappa for pass/fail: 1 = perfect, 0 = no better than chance."""
        n = self.total
        if not n:
            return 0.0
        judge_pass = sum(1 for _, s in self.items if s.passed) / n
        human_pass = sum(1 for i, _ in self.items if i.human_pass) / n
        expected = judge_pass * human_pass + (1 - judge_pass) * (1 - human_pass)
        if expected >= 1.0:
            return 1.0 if self.agreement == 1.0 else 0.0
        return (self.agreement - expected) / (1 - expected)

    @property
    def passed(self) -> bool:
        return (self.total > 0 and self.agreement >= self.min_agreement - 1e-9
                and len(self.false_passes) <= self.max_false_pass)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed, "items": self.total, "agreement": round(self.agreement, 3),
            "kappa": round(self.kappa, 3), "false_passes": self.false_passes, "false_fails": self.false_fails,
            "details": [{"id": i.id, "kind": i.kind, "human": "pass" if i.human_pass else "fail",
                         "judge_score": s.score, "judge": "pass" if s.passed else "fail", "reason": s.reason}
                        for i, s in self.items],
        }

    def to_markdown(self) -> str:
        status = "✅ Judge calibration: PASSED" if self.passed else "❌ Judge calibration: FAILED"
        lines = [f"## {status}", "",
                 f"Agreement with humans **{self.agreement:.0%}** ({self.agreements}/{self.total}), "
                 f"kappa **{self.kappa:.2f}**; false passes **{len(self.false_passes)}** "
                 f"(allowed {self.max_false_pass}), false fails {len(self.false_fails)}. "
                 f"Minimum agreement {self.min_agreement:.0%}.", "",
                 "| Item | Kind | Human | Judge | Score | Reason |", "|---|---|---|---|---|---|"]
        for item, score in self.items:
            mark = "" if score.passed == item.human_pass else (" ⚠️ false pass" if score.passed else " false fail")
            reason = (score.reason or "").replace("|", "/").replace("\n", " ")[:120]
            lines.append(f"| `{item.id}` | {item.kind} | {'pass' if item.human_pass else 'fail'} | "
                         f"{'pass' if score.passed else 'fail'}{mark} | {score.score if score.score is not None else 'n/a'} | {reason} |")
        return "\n".join(lines) + "\n"


def load_calibration(path: str | Path) -> list[CalibrationItem]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    items, ids = [], set()
    for raw in data.get("items", []):
        kind = str(raw.get("kind", "rubric"))
        if kind not in ("rubric", "grounded"):
            raise ValueError(f"item {raw.get('id')}: kind must be 'rubric' or 'grounded'")
        if kind == "rubric" and not raw.get("rubric"):
            raise ValueError(f"item {raw.get('id')}: a rubric item needs 'rubric'")
        human = str(raw.get("human", "")).lower()
        if human not in ("pass", "fail"):
            raise ValueError(f"item {raw.get('id')}: human must be 'pass' or 'fail'")
        item = CalibrationItem(id=str(raw["id"]), kind=kind, query=str(raw["query"]), response=str(raw["response"]),
                               human_pass=human == "pass", rubric=raw.get("rubric"), context=raw.get("context"),
                               threshold=int(raw.get("threshold", 4)))
        if item.id in ids:
            raise ValueError(f"duplicate calibration id: {item.id}")
        ids.add(item.id)
        items.append(item)
    return items


async def run_calibration(judge: Judge, items: list[CalibrationItem], *, min_agreement: float = 0.8,
                          max_false_pass: int = 0) -> CalibrationReport:
    report = CalibrationReport(min_agreement=min_agreement, max_false_pass=max_false_pass)
    for item in items:
        rubric = item.rubric if item.kind == "rubric" else GROUNDEDNESS_RUBRIC
        # grounded items always get a context block, so "no tools were called" is explicit to the judge
        context = item.context if item.kind == "rubric" else (item.context or "")
        score = await judge.score(item.kind, rubric, query=item.query, response=item.response,
                                  context=context, threshold=item.threshold)
        report.items.append((item, score))
    return report
