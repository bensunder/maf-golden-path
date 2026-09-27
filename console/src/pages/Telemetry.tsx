import { Activity, ExternalLink, RotateCw, Table2 } from "lucide-react";
import { useState } from "react";

import { compact, fillBins, Segmented, TimeChart, type Point } from "@/components/charts";
import { Button, ButtonLink } from "@/components/ui/button";
import { Card, CardBody, CardHeader, Eyebrow, KeyValue } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/overlay";
import { Page, PageHeader, SectionTitle } from "@/components/ui/page";
import { EmptyState, ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, StatusDot } from "@/components/ui/status";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { api, type Overview, type Traffic, type TrafficPoint, type TrafficRange } from "@/lib/api";
import { safeUrl } from "@/lib/agui";
import { useLoad, useOverview } from "@/lib/data";
import { cn, duration, timeAgo } from "@/lib/format";

const RANGE_OPTIONS: { value: TrafficRange; label: string }[] = [
  { value: "1h", label: "Last hour" },
  { value: "24h", label: "24 hours" },
  { value: "7d", label: "7 days" },
];
const RANGE_MINUTES: Record<TrafficRange, number> = { "1h": 60, "24h": 1440, "7d": 10080 };

export function TelemetryPage() {
  const overview = useOverview();
  return (
    <Page>
      <PageHeader title="Telemetry" description="Monitor agent executions, tools, latency and errors." />
      {overview.error && !overview.data ? (
        <Card>
          <ErrorState title="Unable to load telemetry" message={overview.error.message} onRetry={overview.reload} />
        </Card>
      ) : !overview.data ? (
        <LoadingRegion label="Loading telemetry">
          <Skeleton className="h-64 w-full rounded-lg" />
        </LoadingRegion>
      ) : !overview.data.telemetry.exporter ? (
        <Card>
          <EmptyState icon={<Activity aria-hidden />} title="Telemetry not connected">
            Connect OpenTelemetry to view production traces and metrics. Set{" "}
            <span className="font-mono">APPLICATIONINSIGHTS_CONNECTION_STRING</span> (the Azure deployment does) or{" "}
            <span className="font-mono">AGENTKIT_OTLP_ENDPOINT</span>.
          </EmptyState>
        </Card>
      ) : (
        <TelemetryBody data={overview.data} />
      )}
    </Page>
  );
}

function TelemetryBody({ data }: { data: Overview }) {
  const [range, setRange] = useState<TrafficRange>("24h");
  const [tables, setTables] = useState(false);
  const traffic = useLoad(() => api.traffic(range), [range]);
  const workbook = safeUrl(data.telemetry.workbook_url);
  const t = traffic.data;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2">
        <Segmented label="Time range" value={range} options={RANGE_OPTIONS} onChange={setRange} />
        <Button size="sm" variant="ghost" onClick={traffic.reload} disabled={traffic.loading} aria-label="Refresh telemetry">
          <RotateCw aria-hidden className={cn(traffic.loading && "animate-spin motion-reduce:animate-none")} /> Refresh
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setTables((v) => !v)} aria-pressed={tables}>
          <Table2 aria-hidden /> {tables ? "Hide tables" : "Show as tables"}
        </Button>
        {t?.queried_at && <span className="text-xs text-zinc-500">Queried {timeAgo(t.queried_at)} · refreshes at most once a minute</span>}
        {workbook && (
          <ButtonLink size="sm" href={workbook} external className="ml-auto">
            <ExternalLink aria-hidden /> Operations workbook
          </ButtonLink>
        )}
      </div>

      {traffic.error && !t ? (
        <Card>
          <ErrorState title="Unable to load telemetry" message={traffic.error.message} onRetry={traffic.reload} />
        </Card>
      ) : !t ? (
        <LoadingRegion label="Loading telemetry" className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} className="h-24 rounded-lg" />
            ))}
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            {[0, 1].map((i) => (
              <Skeleton key={i} className="h-56 rounded-lg" />
            ))}
          </div>
        </LoadingRegion>
      ) : !t.available ? (
        <Card>
          {t.error ? (
            <ErrorState title="Unable to load telemetry" message={t.error} onRetry={traffic.reload} />
          ) : (
            <EmptyState icon={<Activity aria-hidden />} title="Live charts not connected">
              {t.reason} Until then, the operations workbook shows this agent's traffic.
            </EmptyState>
          )}
        </Card>
      ) : (
        <div className={cn("space-y-6 transition-opacity", traffic.loading && "opacity-60")}>
          <LiveTraffic traffic={t} range={range} tables={tables} />
        </div>
      )}

      <Card>
        <CardHeader title="Export" description="Telemetry leaves the service as it happens; the charts above query it back from Azure Monitor." />
        <CardBody>
          <dl className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <KeyValue label="Exporter">{data.telemetry.exporter === "app_insights" ? "Azure Monitor (Application Insights)" : "OTLP"}</KeyValue>
            <KeyValue label="Status">
              <span title="An exporter is configured. Delivery isn't checked from here; the charts above show what arrived.">
                <StatusDot tone="ok">Configured</StatusDot>
              </span>
            </KeyValue>
            <KeyValue label="Prompt content">{data.telemetry.capture_content ? "Recorded in traces" : "Not recorded"}</KeyValue>
          </dl>
        </CardBody>
      </Card>
    </div>
  );
}

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card className="px-4 py-3.5">
      <Eyebrow>{label}</Eyebrow>
      <div className="mt-1.5 text-xl font-semibold tabular-nums tracking-tight text-zinc-950">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-zinc-500">{sub}</div>}
    </Card>
  );
}

function LiveTraffic({ traffic, range, tables }: { traffic: Traffic; range: TrafficRange; tables: boolean }) {
  const bin = traffic.bin_minutes ?? 60;
  const totals = traffic.totals!;
  const filled = fillBins<TrafficPoint>(traffic.series ?? [], bin, RANGE_MINUTES[range]);
  const pts = (pick: (p: TrafficPoint | null) => number | null): Point[] =>
    filled.map((p, i) => ({ t: filled.times![i], v: pick(p), partial: filled.partial![i] }));
  const noRuns = totals.runs === 0 && totals.tokens === 0;

  return (
    <>
      <section aria-label="Totals" className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Kpi label="Requests" value={compact(totals.runs)} sub={totals.blocked ? `${compact(totals.blocked)} refused by guardrails` : "agent runs"} />
        <Kpi
          label="Errors"
          value={compact(totals.errors)}
          sub={totals.error_rate === null ? "no runs" : `${(totals.error_rate * 100).toFixed(totals.error_rate < 0.1 ? 1 : 0)}% of runs`}
        />
        <Kpi label="Latency" value={totals.avg_s === null ? "—" : duration(totals.avg_s)} sub={totals.max_s === null ? "average" : `average · max ${duration(totals.max_s)}`} />
        <Kpi label="Tool calls" value={compact(totals.tool_calls)} sub={(traffic.tools?.length ?? 0) >= 20 ? "top 20 tools listed below" : `${traffic.tools?.length ?? 0} tools used`} />
        <Kpi label="Tokens" value={compact(totals.tokens)} sub="through the AI gateway" />
      </section>

      {noRuns && (
        <InlineNotice>
          No runs in this period. Telemetry reaches Azure Monitor a few minutes after a run; try a longer range or send a message in the
          playground.
        </InlineNotice>
      )}

      <p className="-mb-2 text-xs text-zinc-500">
        Lighter bars cover part of an interval: the oldest one starts inside the range, and the latest is still filling in
        (telemetry arrives a few minutes after each run).
      </p>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <ChartCard title="Requests" description={`Agent runs per ${binLabel(bin)}`}>
          <TimeChart kind="bar" label="Runs" binMinutes={bin} points={pts((p) => p?.runs ?? 0)} showTable={tables} />
        </ChartCard>
        <ChartCard title="Errors" description={`Failed runs per ${binLabel(bin)}`}>
          <TimeChart kind="bar" label="Errors" binMinutes={bin} points={pts((p) => p?.errors ?? 0)} showTable={tables} />
        </ChartCard>
        <ChartCard title="Latency" description="Average run time, seconds">
          <TimeChart kind="line" label="Average run time" binMinutes={bin} points={pts((p) => p?.avg_s ?? null)} format={(v) => `${compact(v)} s`} showTable={tables} />
        </ChartCard>
        <ChartCard title="Tokens" description={`Model tokens per ${binLabel(bin)}, measured at the gateway`}>
          <TimeChart kind="bar" label="Tokens" binMinutes={bin} points={pts((p) => p?.tokens ?? 0)} showTable={tables} />
        </ChartCard>
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <Card>
          <CardHeader title="Tool calls" description="Every tool call is a span; failures are calls that raised." />
          {(traffic.tools ?? []).length === 0 ? (
            <EmptyState compact title="No tool calls in this period" />
          ) : (
            <Table label="Tool calls">
              <thead>
                <tr>
                  <Th>Tool</Th>
                  <Th className="text-right">Calls</Th>
                  <Th className="text-right">Failures</Th>
                  <Th className="hidden text-right sm:table-cell">Avg</Th>
                  <Th className="hidden text-right sm:table-cell">Max</Th>
                </tr>
              </thead>
              <tbody>
                {traffic.tools!.map((row) => (
                  <Tr key={row.tool}>
                    <Td className="font-mono text-[13px] text-zinc-900">{row.tool}</Td>
                    <Td className="text-right tabular-nums">{compact(row.calls)}</Td>
                    <Td className="text-right tabular-nums">
                      {row.failures ? <StatusBadge tone="bad">{row.failures}</StatusBadge> : <span className="text-zinc-500">0</span>}
                    </Td>
                    <Td className="hidden text-right tabular-nums sm:table-cell">{row.avg_ms === null ? "—" : duration(row.avg_ms / 1000)}</Td>
                    <Td className="hidden text-right tabular-nums sm:table-cell">{row.max_ms === null ? "—" : duration(row.max_ms / 1000)}</Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>

        <Card>
          <CardHeader title="Recent traces" description="The last 20 agent runs. Search the operation ID in Application Insights for the full trace." />
          {(traffic.recent ?? []).length === 0 ? (
            <EmptyState compact title="No traces in this period" />
          ) : (
            <Table label="Recent traces">
              <thead>
                <tr>
                  <Th>Operation ID</Th>
                  <Th>When</Th>
                  <Th className="text-right">Duration</Th>
                  <Th>Status</Th>
                </tr>
              </thead>
              <tbody>
                {traffic.recent!.map((r) => (
                  <Tr key={`${r.operation_id}-${r.time}`}>
                    <Td>
                      <span className="flex items-center gap-1 font-mono text-[13px] text-zinc-900">
                        {(r.operation_id || "").slice(0, 8)}…
                        {r.operation_id && <CopyButton value={r.operation_id} label="Copy operation ID" className="h-6 w-6 [&_svg]:size-3.5" />}
                      </span>
                    </Td>
                    <Td className="whitespace-nowrap">{timeAgo(Date.parse(r.time) / 1000)}</Td>
                    <Td className="text-right tabular-nums">{r.duration_ms === null ? "—" : duration(r.duration_ms / 1000)}</Td>
                    <Td>
                      {r.success === false ? (
                        <StatusBadge tone="bad">Failed</StatusBadge>
                      ) : r.success === true ? (
                        <StatusBadge tone="ok">OK</StatusBadge>
                      ) : (
                        <StatusBadge tone="neutral">Unknown</StatusBadge>
                      )}
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
      </div>
      <SectionTitle>How these are measured</SectionTitle>
      <p className="-mt-2 text-[13px] leading-relaxed text-zinc-500">
        Requests, errors and latency come from the <span className="font-mono">agentkit.agent.runs</span> and{" "}
        <span className="font-mono">agentkit.agent.run.duration</span> metrics this agent emits; tokens from the gateway's{" "}
        <span className="font-mono">Total Tokens</span> metric for this agent and environment; tools and traces from its spans. The service
        queries Azure Monitor with its own identity.
      </p>
    </>
  );
}

function ChartCard({ title, description, children }: { title: string; description: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader title={title} description={description} />
      <div className="px-3 pb-3 pt-2">{children}</div>
    </Card>
  );
}

function binLabel(minutes: number): string {
  if (minutes < 60) return `${minutes} minutes`;
  if (minutes === 60) return "hour";
  return `${minutes / 60} hours`;
}
