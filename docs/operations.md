# Operations: dashboard, alerts, metrics, judge calibration

The platform deploys one dashboard and a set of alerts that cover **every agent service** on it. Teams don't build their own, because every service emits the same metrics and every model call goes through the same gateway.

## The dashboard

**Azure portal → Monitor → Workbooks → "agentkit operations (<prefix>)"**, with a time-range picker:

| Panel | Answers |
|---|---|
| Model tokens by team | Who is spending, over time (from the gateway, so no service can under-report) |
| Tokens by team, agent and environment | Chargeback |
| Agent runs by outcome | ok / blocked / error over time |
| Refused inputs by reason and agent | Prompt injection vs. token budget vs. over-long input |
| Agent run time | Average and worst run time |
| Error rate by agent | Which agent is failing |
| Human approvals | Requested vs. decided, by tool and decision |
| Knowledge searches by outcome | `unavailable` means users got answers without documents (Search, Graph or permissions) |

## Alerts

| Alert | Fires when | Severity |
|---|---|---|
| Model token spend spike | > 2M tokens through the gateway in an hour | 2 |
| Prompt-injection attempts spike | > 20 refusals for prompt injection in 15 minutes | 2 |
| Agent error rate above 5% | > 5% of runs failed in 15 minutes (with at least 20 runs) | 1 |
| Approvals piling up | > 10 more approvals requested than decided in 4 hours | 3 |
| Knowledge search failing closed | > 5 `unavailable` searches in 15 minutes | 2 |

Set `alertEmail` on the platform deployment to get emails; otherwise alerts show in the portal. Thresholds are in `infra/platform/ops/queries.json`.

## Changing a panel or an alert

1. Edit `infra/platform/ops/queries.json` (the only place queries live).
2. `python scripts/build_workbook.py` regenerates the workbook. CI fails if you forget.
3. Deploy the platform. The next live validation runs every query against the real workspace.

Alert queries must produce a `Value` column (the rule sums it); `scripts/check_infra.py` checks this.

## Metrics reference

| Metric | Dimensions | From |
|---|---|---|
| `Total Tokens` (and prompt/completion) | `Team`, `Agent`, `Environment`, `Caller` | The AI gateway (`llm-emit-token-metric`) |
| `agentkit.agent.runs` | `gen_ai.agent.name`, `outcome` (ok/blocked/error), `blocked_reason`, `stream` | Every agent (`AgentRunMetricsMiddleware`) |
| `agentkit.agent.run.duration` (s) | as above | Every agent |
| `agentkit.approvals.requested` / `.decided` | `tool`, `decision` | The conversation service |
| `agentkit.knowledge.searches` | `outcome` (results/empty/unavailable), `index` | The knowledge tool |

Traces carry user (pseudonymized), session, tenant and team on every span ([telemetry.md](telemetry.md)).

## Judge calibration

The deploy gate trusts an LLM judge to score answers. Check that trust before relying on it, and again whenever the judge model changes:

```bash
agentkit-gate --calibrate evals/judge_calibration.yaml        # gateway settings from the environment
```

A calibration file holds a few dozen answers a person has graded (`human: pass|fail`), for rubric and groundedness checks. The report gives:

- **agreement** with the humans (must be at least `--min-agreement`, default 80%);
- **Cohen's kappa**, which is agreement beyond chance;
- **false passes**, answers the judge passed that a person failed. These let regressions through the gate, so the default allows none (`--max-false-pass 0`). False fails are only noise.

The sample ships 12 graded answers in `examples/order-status-agent/evals/judge_calibration.yaml`. Keep your set balanced, and add an item whenever the judge gets one wrong. Live validation runs calibration on every run.
