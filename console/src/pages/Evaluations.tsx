import { CircleCheck, CircleX, ClipboardCheck, Lock, RotateCw, Terminal } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, Eyebrow } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/overlay";
import { Page, PageHeader, SectionTitle } from "@/components/ui/page";
import { EmptyState, ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, StatusIcon, Tag } from "@/components/ui/status";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { api, type EvalCase, type GateReport } from "@/lib/api";
import { useLoad } from "@/lib/data";
import { dateTime, duration, humanize, timeAgo } from "@/lib/format";

const RUN_OFFLINE = "agentkit-gate --cases evals/cases.yaml --factory <package>.agent:create_agent --report build/gate-report.json";

export function EvaluationsPage() {
  const evals = useLoad(api.evals);
  return (
    <Page>
      <PageHeader
        title="Evaluations"
        description="Verify agent behavior before production. The same cases gate every deploy."
        actions={
          <Button size="sm" onClick={evals.reload} aria-label="Refresh evaluations">
            <RotateCw aria-hidden /> Refresh
          </Button>
        }
      />
      {evals.error && !evals.data ? (
        <Card>
          <ErrorState title="Unable to load evaluations" message={evals.error.message} onRetry={evals.reload} />
        </Card>
      ) : !evals.data ? (
        <LoadingRegion label="Loading evaluations" className="space-y-6">
          <Card className="p-6">
            <Skeleton className="h-4 w-32" />
            <Skeleton className="mt-4 h-8 w-40" />
          </Card>
          <Card className="space-y-3 p-6">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-4 w-full" />
            ))}
          </Card>
        </LoadingRegion>
      ) : (
        <div className="space-y-6">
          {evals.data.report_error && <InlineNotice tone="warn">{evals.data.report_error}</InlineNotice>}
          {evals.data.report ? <GateSummary report={evals.data.report} cases={evals.data.cases ?? []} /> : <NoReport />}
          <CasesSection cases={evals.data.cases} error={evals.data.cases_error} report={evals.data.report} />
        </div>
      )}
    </Page>
  );
}

function GateSummary({ report, cases }: { report: GateReport; cases: EvalCase[] }) {
  const passedRuns = report.cases.reduce((n, c) => n + c.passed_runs, 0);
  const runs = report.cases.reduce((n, c) => n + c.runs, 0);
  const passedCases = report.cases.filter((c) => c.passed_runs === c.runs && c.runs > 0).length;
  // Which kinds of checks the cases cover (from the case definitions, not the report).
  const kinds = Array.from(new Set(cases.flatMap((c) => c.checks))).sort();
  return (
    <Card>
      <CardHeader
        title="Quality gate"
        description={
          <>
            {report.live ? "Live" : "Offline (scripted model)"} run · {report.repeat}× each · {timeAgo(report.started_at)} ({dateTime(report.started_at)})
          </>
        }
        action={<StatusBadge tone={report.passed ? "ok" : "bad"}>{report.passed ? "Passed" : "Failed"}</StatusBadge>}
      />
      <CardBody className="grid gap-8 lg:grid-cols-[260px_1fr]">
        <div>
          <div className="text-3xl font-semibold tracking-tight text-zinc-950 tabular-nums">
            {passedRuns} <span className="text-zinc-500">/</span> {runs}
          </div>
          <div className="mt-1 text-[13px] text-zinc-500">runs passing</div>
          <dl className="mt-5 grid grid-cols-2 gap-4 text-sm">
            <div>
              <dt>
                <Eyebrow>Cases</Eyebrow>
              </dt>
              <dd className="mt-1 tabular-nums text-zinc-900">
                {passedCases} of {report.cases.length}
              </dd>
            </div>
            <div>
              <dt>
                <Eyebrow>Duration</Eyebrow>
              </dt>
              <dd className="mt-1 text-zinc-900">{duration(report.duration_s)}</dd>
            </div>
            <div>
              <dt>
                <Eyebrow>Mean pass rate</Eyebrow>
              </dt>
              <dd className="mt-1 tabular-nums text-zinc-900">{Math.round(report.pass_rate * 100)}%</dd>
            </div>
            <div>
              <dt>
                <Eyebrow>Baseline</Eyebrow>
              </dt>
              <dd className="mt-1 text-zinc-900">{report.baseline_used ? "Compared" : "None"}</dd>
            </div>
          </dl>
        </div>
        <div>
          {report.reasons.length > 0 && (
            <InlineNotice tone="bad" className="mb-4">
              <div className="font-medium">Why it failed</div>
              <ul className="mt-1 list-disc pl-4">
                {report.reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </InlineNotice>
          )}
          <Eyebrow>Checks covered by the cases</Eyebrow>
          <ul className="mt-2.5 grid gap-x-6 gap-y-2 sm:grid-cols-2">
            {kinds.map((k) => (
              <li key={k} className="flex items-center gap-2 text-sm text-zinc-800">
                <CircleCheck aria-hidden className="size-4 text-zinc-500" /> {k}
              </li>
            ))}
          </ul>
          <p className="mt-4 text-xs text-zinc-500">
            {report.live
              ? "Live runs call the real model through the gateway; rubric and groundedness are scored by the judge."
              : "Offline runs replay each case's scripted model turns through the real agent, tools, guardrails and approvals. Judge checks run live only."}
          </p>
        </div>
      </CardBody>
    </Card>
  );
}

function NoReport() {
  return (
    <Card>
      <EmptyState icon={<ClipboardCheck aria-hidden />} title="No evaluation results in this environment">
        The quality gate runs in the deploy pipeline and posts results on the run page. To show a run here, write a report and point{" "}
        <span className="font-mono">AGENTKIT_CONSOLE_EVAL_REPORT</span> at it:
        <span className="mt-3 flex items-center gap-1 rounded-md border border-zinc-200 bg-zinc-50 py-1 pl-3 pr-1 text-left font-mono text-xs text-zinc-700">
          <Terminal aria-hidden className="size-3.5 shrink-0 text-zinc-500" />
          <span className="min-w-0 flex-1 truncate">{RUN_OFFLINE}</span>
          <CopyButton value={RUN_OFFLINE} label="Copy command" />
        </span>
      </EmptyState>
    </Card>
  );
}

function CasesSection({ cases, error, report }: { cases: EvalCase[] | null; error: string | null; report: GateReport | null }) {
  const byId = new Map((report?.cases ?? []).map((c) => [c.id, c]));
  return (
    <section aria-labelledby="cases-title">
      <SectionTitle id="cases-title">Evaluation cases</SectionTitle>
      <Card>
        {error ? (
          <ErrorState compact title="Unable to read the eval cases" message={error} />
        ) : !cases || cases.length === 0 ? (
          <EmptyState compact icon={<ClipboardCheck aria-hidden />} title="No eval cases found">
            Add cases to <span className="font-mono">evals/cases.yaml</span>. Every bug fix should add one.
          </EmptyState>
        ) : (
          <Table label="Evaluation cases">
            <thead>
              <tr>
                <Th>Test case</Th>
                <Th>Status</Th>
                <Th className="hidden md:table-cell">Checks</Th>
                <Th className="hidden sm:table-cell text-right">Duration</Th>
              </tr>
            </thead>
            <tbody>
              {cases.map((c) => {
                const r = byId.get(c.id);
                const passed = r && r.runs > 0 && r.passed_runs === r.runs;
                const skipped = r && r.runs === 0;
                return (
                  <Tr key={c.id}>
                    <Td className="max-w-[420px]">
                      <div className="flex items-center gap-2">
                        <span className="text-zinc-900">{humanize(c.id)}</span>
                        {c.critical && (
                          <span title="Critical: must pass every run">
                            <Lock aria-label="Critical" className="size-3.5 text-zinc-500" />
                          </span>
                        )}
                        {c.as_user && <Tag>as user</Tag>}
                      </div>
                      <div className="mt-0.5 truncate text-xs text-zinc-500" title={c.input}>
                        {c.input}
                      </div>
                      {r && r.failures.length > 0 && <div className="mt-1 text-xs text-red-700">{r.failures[0]}</div>}
                    </Td>
                    <Td className="whitespace-nowrap">
                      {!r ? (
                        <span className="text-[13px] text-zinc-500">Not run</span>
                      ) : skipped ? (
                        <StatusBadge tone="neutral">Skipped</StatusBadge>
                      ) : passed ? (
                        <span className="inline-flex items-center gap-1.5 text-[13px] font-medium text-emerald-700">
                          <StatusIcon tone="ok" label="Passed" /> Pass
                          {r.runs > 1 && <span className="font-normal text-zinc-500">{r.passed_runs}/{r.runs}</span>}
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1.5 text-[13px] font-medium text-red-700">
                          <CircleX aria-hidden className="size-4" /> Fail
                          <span className="font-normal text-zinc-500">
                            {r.passed_runs}/{r.runs}
                          </span>
                        </span>
                      )}
                    </Td>
                    <Td className="hidden md:table-cell">
                      <div className="flex max-w-[320px] flex-wrap gap-1">
                        {c.checks.map((k) => (
                          <Tag key={k}>{k}</Tag>
                        ))}
                      </div>
                    </Td>
                    <Td className="hidden whitespace-nowrap text-right tabular-nums sm:table-cell">{r?.mean_duration_s != null ? duration(r.mean_duration_s) : "—"}</Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
    </section>
  );
}
