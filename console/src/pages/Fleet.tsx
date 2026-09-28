// Fleet mode: served by the fleet service, one row per agent service. Every value comes from the agents'
// own console APIs (asked server-side by the fleet service) and from Azure Monitor.
import * as DialogPrimitive from "@radix-ui/react-dialog";
import {
  Activity,
  BookOpen,
  CircleCheck,
  CircleMinus,
  ClipboardCheck,
  ExternalLink,
  LayoutGrid,
  Menu,
  RotateCw,
  Shield,
  TriangleAlert,
  X,
  type LucideIcon,
} from "lucide-react";
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { compact, Segmented } from "@/components/charts";
import { NavLink, Wordmark } from "@/components/shell/AppShell";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Page, PageHeader } from "@/components/ui/page";
import { EmptyState, ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, StatusDot, Tag, type Tone } from "@/components/ui/status";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { fleetApi, type Fleet, type FleetAgent, type FleetTraffic, type TrafficRange } from "@/lib/api";
import { safeUrl } from "@/lib/agui";
import { useLoad, type Loadable } from "@/lib/data";
import { cn, dateTime, duration, environmentLabel, timeAgo } from "@/lib/format";
import { RouterProvider, useRouter } from "@/lib/router";

// ------------------------------------------------------------------ data
const FleetContext = createContext<Loadable<Fleet> | null>(null);

function useFleet(): Loadable<Fleet> {
  const ctx = useContext(FleetContext);
  if (!ctx) throw new Error("useFleet outside FleetApp");
  return ctx;
}

function FleetProvider({ children }: { children: ReactNode }) {
  const value = useLoad(fleetApi.agents);
  const { reload } = value;
  useEffect(() => {
    const timer = window.setInterval(() => document.visibilityState === "visible" && reload(), 60_000);
    return () => window.clearInterval(timer);
  }, [reload]);
  return <FleetContext.Provider value={value}>{children}</FleetContext.Provider>;
}

function health(a: FleetAgent): { tone: Tone; label: string } {
  if (a.status === "unreachable") return { tone: "bad", label: "Unreachable" };
  if (a.status === "not_ready") return { tone: "bad", label: "Not ready" };
  return { tone: "ok", label: "Ready" };
}

function posture(a: FleetAgent) {
  const controls = a.overview?.security ?? [];
  const on = controls.filter((c) => c.status === "on").length;
  return { on, total: controls.length, allOn: controls.length > 0 && on === controls.length };
}

function consoleNote(a: FleetAgent): string | null {
  switch (a.console) {
    case "not_installed":
      return "No console on this service";
    case "denied":
      return "Console refused the fleet's identity";
    case "no_token":
      return "No token for this agent";
    case "unreachable":
    case "error":
      return "Console didn't answer";
    default:
      return null;
  }
}

// ------------------------------------------------------------------ shell
const NAV: { to: string; label: string; icon: LucideIcon; match?: (p: string) => boolean }[] = [
  { to: "/", label: "Fleet", icon: LayoutGrid, match: (p) => p === "/" },
  { to: "/security", label: "Security", icon: Shield },
  { to: "/quality", label: "Quality", icon: ClipboardCheck },
  { to: "/traffic", label: "Traffic", icon: Activity },
];

function FleetShell({ children }: { children: ReactNode }) {
  const { path } = useRouter();
  const [open, setOpen] = useState(false);
  const me = useLoad(fleetApi.me);
  const nav = (onNavigate?: () => void) => (
    <nav aria-label="Main" className="px-3">
      <ul className="space-y-0.5">
        {NAV.map((item) => (
          <li key={item.to}>
            <NavLink item={item} path={path} onNavigate={onNavigate} />
          </li>
        ))}
      </ul>
      <div className="my-4 border-t border-zinc-200" />
      <a
        href="https://github.com/bensunder/maf-golden-path/blob/main/docs/fleet.md"
        target="_blank"
        rel="noopener noreferrer"
        className="flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13.5px] text-zinc-600 hover:bg-zinc-200/50 hover:text-zinc-900"
      >
        <BookOpen aria-hidden className="size-4 text-zinc-500" /> <span className="flex-1">Documentation</span>
        <ExternalLink aria-hidden className="size-3.5 text-zinc-500" />
        <span className="sr-only">(opens in a new tab)</span>
      </a>
    </nav>
  );
  return (
    <div className="flex min-h-dvh bg-canvas">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2">
        Skip to content
      </a>
      <aside className="sticky top-0 hidden h-dvh w-60 shrink-0 border-r border-zinc-200 bg-zinc-50 lg:block">
        <div className="flex h-14 items-center px-5">
          <Wordmark />
        </div>
        {nav()}
      </aside>
      <DialogPrimitive.Root open={open} onOpenChange={setOpen}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-zinc-950/30 lg:hidden" />
          <DialogPrimitive.Content className="fixed inset-y-0 left-0 z-50 w-72 border-r border-zinc-200 bg-zinc-50 pt-14 focus:outline-none lg:hidden">
            <DialogPrimitive.Title className="sr-only">Navigation</DialogPrimitive.Title>
            <DialogPrimitive.Description className="sr-only">Main navigation</DialogPrimitive.Description>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon" className="absolute right-3 top-3" aria-label="Close navigation">
                <X aria-hidden />
              </Button>
            </DialogPrimitive.Close>
            {nav(() => setOpen(false))}
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-zinc-200 bg-white/85 px-4 backdrop-blur sm:px-6">
          <Button variant="ghost" size="icon" className="lg:hidden" aria-label="Open navigation" onClick={() => setOpen(true)}>
            <Menu aria-hidden />
          </Button>
          <div className="lg:hidden">
            <Wordmark className="[&>span:last-child]:hidden sm:[&>span:last-child]:inline" />
          </div>
          <span className="hidden text-[13px] font-medium text-zinc-900 lg:inline">{me.data?.title ?? "Agent fleet"}</span>
          <div className="ml-auto flex items-center gap-2">
            {me.data && (
              <span className="hidden items-center gap-2 rounded-full border border-zinc-200 bg-white px-2.5 py-1 text-xs font-medium text-zinc-700 sm:inline-flex">
                <span aria-hidden className="size-1.5 rounded-full bg-accent-500" />
                <span className="sr-only">Environment: </span>
                {environmentLabel(me.data.environment)}
              </span>
            )}
            {me.data?.user && <span className="max-w-[200px] truncate text-[13px] text-zinc-600">{me.data.user}</span>}
          </div>
        </header>
        <main id="main" tabIndex={-1} className="flex-1 focus:outline-none">
          {children}
        </main>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ shared
function FleetGate({ title, children }: { title: string; children: (fleet: Fleet) => ReactNode }) {
  const fleet = useFleet();
  if (fleet.error && !fleet.data)
    return (
      <Card>
        <ErrorState title={`Unable to load ${title.toLowerCase()}`} message={fleet.error.message} onRetry={fleet.reload} />
      </Card>
    );
  if (!fleet.data)
    return (
      <LoadingRegion label={`Loading ${title.toLowerCase()}`} className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-24 rounded-lg" />
          ))}
        </div>
        <Skeleton className="h-64 rounded-lg" />
      </LoadingRegion>
    );
  if (fleet.data.agents.length === 0)
    return (
      <Card>
        <EmptyState icon={<LayoutGrid aria-hidden />} title="No agents registered">
          Add agent services to the registry (<span className="font-mono">AGENTKIT_FLEET_REGISTRY</span> or{" "}
          <span className="font-mono">AGENTKIT_FLEET_AGENTS</span>), or turn on Azure discovery with{" "}
          <span className="font-mono">AGENTKIT_FLEET_DISCOVER=true</span>.
          {fleet.data.discovery_error && <span className="mt-2 block text-red-700">{fleet.data.discovery_error}</span>}
        </EmptyState>
      </Card>
    );
  return <>{children(fleet.data)}</>;
}

function RefreshButton() {
  const fleet = useFleet();
  return (
    <Button size="sm" onClick={fleet.reload} disabled={fleet.loading} aria-label="Check all agents again">
      <RotateCw aria-hidden className={cn(fleet.loading && "animate-spin motion-reduce:animate-none")} /> Check again
    </Button>
  );
}

function AgentName({ a }: { a: FleetAgent }) {
  const host = (() => {
    try {
      // where people open it (the fleet may reach it at a private address)
      return new URL(a.console_url || a.url).host;
    } catch {
      return a.url;
    }
  })();
  return (
    <div className="min-w-0">
      <div className="truncate font-medium text-zinc-950">{a.name ?? host}</div>
      <div className="truncate font-mono text-xs text-zinc-500">{host}</div>
    </div>
  );
}

function ConsoleLink({ a }: { a: FleetAgent }) {
  const href = safeUrl(a.console_url);
  if (!href || a.console !== "ok") return null;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 whitespace-nowrap rounded text-[13px] font-medium text-accent-700 hover:text-accent-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
    >
      Console <ExternalLink aria-hidden className="size-3.5" />
      <span className="sr-only">for {a.name} (opens in a new tab)</span>
    </a>
  );
}

function Kpi({ label, value, sub, tone }: { label: string; value: string; sub: string; tone?: Tone }) {
  return (
    <Card className="px-5 py-4">
      <div className="text-[13px] font-medium text-zinc-500">{label}</div>
      <div className="mt-2 flex items-baseline gap-2">
        <span className="text-2xl font-semibold tabular-nums tracking-tight text-zinc-950">{value}</span>
        {tone && tone !== "ok" && <StatusBadge tone={tone}>Attention</StatusBadge>}
      </div>
      <div className="mt-1 text-[13px] text-zinc-500">{sub}</div>
    </Card>
  );
}

// ------------------------------------------------------------------ pages
function FleetOverviewPage() {
  const fleet = useFleet();
  return (
    <Page wide>
      <PageHeader title="Agent fleet" description="Every governed agent service: health, security posture, quality and deployments." actions={<RefreshButton />} />
      <FleetGate title="Fleet">
        {(data) => {
          const n = data.agents.length;
          const ready = data.agents.filter((a) => a.status === "ready").length;
          const withConsole = data.agents.filter((a) => a.overview);
          const allOn = withConsole.filter((a) => posture(a).allOn).length;
          const reports = data.agents.filter((a) => a.gate?.report);
          const passed = reports.filter((a) => a.gate!.report!.passed).length;
          return (
            <div className="space-y-6">
              {data.discovery_error && <InlineNotice tone="warn">{data.discovery_error}</InlineNotice>}
              <section aria-label="Summary" className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
                <Kpi label="Agents" value={String(n)} sub={data.discovery ? `${data.registered} registered, the rest discovered in Azure` : "from the registry"} />
                <Kpi label="Ready" value={`${ready} / ${n}`} sub={ready === n ? "all answering /readyz" : `${n - ready} not answering`} tone={ready === n ? "ok" : "bad"} />
                <Kpi
                  label="Security controls all on"
                  value={withConsole.length ? `${allOn} / ${withConsole.length}` : "—"}
                  sub={withConsole.length === 0 ? "no agent's console could be read" : withConsole.length < n ? `${n - withConsole.length} without a readable console` : "read from each running agent"}
                  tone={withConsole.length > 0 && allOn === withConsole.length ? "ok" : "warn"}
                />
                <Kpi
                  label="Quality gate passed"
                  value={reports.length ? `${passed} / ${reports.length}` : "—"}
                  sub={reports.length === 0 ? "no agent has a gate report yet" : reports.length < n ? `${n - reports.length} with no gate report` : "for the build each agent runs"}
                  tone={passed === reports.length ? "ok" : "bad"}
                />
              </section>

              <Card className={cn(fleet.loading && "opacity-70 transition-opacity")}>
                <CardHeader title="Agents" description={`Checked ${timeAgo(data.checked_at)}. Each row is asked live; nothing is stored here.`} />
                <Table label="Agents">
                  <thead>
                    <tr>
                      <Th>Agent</Th>
                      <Th>Health</Th>
                      <Th className="hidden md:table-cell">Environment</Th>
                      <Th className="hidden md:table-cell">Version</Th>
                      <Th>Security</Th>
                      <Th className="hidden lg:table-cell">Quality gate</Th>
                      <Th className="hidden xl:table-cell">Deployed</Th>
                      <Th>
                        <span className="sr-only">Console</span>
                      </Th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.agents.map((a) => {
                      const h = health(a);
                      const p = posture(a);
                      const s = a.overview?.service;
                      const note = consoleNote(a);
                      return (
                        <Tr key={a.url}>
                          <Td className="max-w-[260px]">
                            <AgentName a={a} />
                          </Td>
                          <Td>
                            <span title={a.detail ?? undefined}>
                              <StatusDot tone={h.tone}>{h.label}</StatusDot>
                            </span>
                          </Td>
                          <Td className="hidden md:table-cell">{s ? environmentLabel(s.environment) : "—"}</Td>
                          <Td className="hidden font-mono text-[13px] md:table-cell">{s?.version ?? "—"}</Td>
                          <Td>
                            {a.overview ? (
                              <StatusBadge tone={p.allOn ? "ok" : "warn"}>
                                {p.on}/{p.total} on
                              </StatusBadge>
                            ) : (
                              <span className="text-[13px] text-zinc-500" title={a.detail ?? undefined}>
                                {note ?? "—"}
                              </span>
                            )}
                          </Td>
                          <Td className="hidden lg:table-cell">
                            {a.gate?.report ? (
                              <StatusBadge tone={a.gate.report.passed ? "ok" : "bad"}>
                                {a.gate.report.passed ? "Passed" : "Failed"} {a.gate.report.passed_runs}/{a.gate.report.runs}
                              </StatusBadge>
                            ) : (
                              <span className="text-[13px] text-zinc-500">{a.gate ? "No report" : "—"}</span>
                            )}
                          </Td>
                          <Td className="hidden whitespace-nowrap xl:table-cell">
                            {s?.deployed_at ? (
                              <span title={dateTime(Date.parse(s.deployed_at) / 1000)}>
                                {timeAgo(Date.parse(s.deployed_at) / 1000)}
                                {s.commit && <span className="ml-1.5 font-mono text-xs text-zinc-500">{s.commit.slice(0, 7)}</span>}
                              </span>
                            ) : (
                              <span className="text-zinc-500">—</span>
                            )}
                          </Td>
                          <Td className="text-right">
                            <ConsoleLink a={a} />
                          </Td>
                        </Tr>
                      );
                    })}
                  </tbody>
                </Table>
              </Card>
            </div>
          );
        }}
      </FleetGate>
    </Page>
  );
}

const CONTROL_ORDER = [
  "entra_auth",
  "prompt_injection",
  "tool_output",
  "pii",
  "tool_policy",
  "human_approval",
  "session_isolation",
  "token_budget",
  "audit",
  "content_capture",
];
const CONTROL_SHORT: Record<string, string> = {
  entra_auth: "Sign-in",
  prompt_injection: "Injection",
  tool_output: "Tool output",
  pii: "PII",
  tool_policy: "Tool policy",
  human_approval: "Approval",
  session_isolation: "Isolation",
  token_budget: "Budget",
  audit: "Audit",
  content_capture: "No content",
};

function FleetSecurityPage() {
  return (
    <Page wide>
      <PageHeader title="Security" description="Every control on every agent, read from each agent's running middleware stack." actions={<RefreshButton />} />
      <FleetGate title="Security">
        {(data) => {
          const rows = data.agents.filter((a) => a.overview);
          const missing = data.agents.filter((a) => !a.overview);
          return (
            <div className="space-y-4">
              <Card>
                <Table label="Security controls by agent">
                  <thead>
                    <tr>
                      <Th>Agent</Th>
                      {CONTROL_ORDER.map((c) => (
                        <Th key={c} className="text-center">
                          {CONTROL_SHORT[c]}
                        </Th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((a) => {
                      const byId = new Map((a.overview?.security ?? []).map((c) => [c.id, c]));
                      return (
                        <Tr key={a.url}>
                          <Td className="max-w-[220px]">
                            <AgentName a={a} />
                          </Td>
                          {CONTROL_ORDER.map((id) => {
                            const c = byId.get(id);
                            const label = c ? `${c.name}: ${c.status === "on" ? "on" : c.status === "partial" ? "partial" : "off"}. ${c.detail}` : "Not reported";
                            return (
                              <Td key={id} className="text-center">
                                <span role="img" aria-label={label} title={label} className="inline-flex">
                                  {!c ? (
                                    <span className="text-zinc-400">—</span>
                                  ) : c.status === "on" ? (
                                    <CircleCheck aria-hidden className="size-[18px] text-emerald-600" />
                                  ) : c.status === "partial" ? (
                                    <TriangleAlert aria-hidden className="size-[18px] text-amber-600" />
                                  ) : (
                                    <CircleMinus aria-hidden className="size-[18px] text-zinc-500" />
                                  )}
                                </span>
                              </Td>
                            );
                          })}
                        </Tr>
                      );
                    })}
                  </tbody>
                </Table>
              </Card>
              <div className="flex flex-wrap items-center gap-4 text-xs text-zinc-600">
                <span className="inline-flex items-center gap-1.5">
                  <CircleCheck aria-hidden className="size-4 text-emerald-600" /> On
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <TriangleAlert aria-hidden className="size-4 text-amber-600" /> Partial (hover for why)
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <CircleMinus aria-hidden className="size-4 text-zinc-500" /> Off
                </span>
              </div>
              {missing.length > 0 && (
                <InlineNotice tone="warn">
                  Not shown: {missing.map((a) => `${a.name ?? a.url} (${consoleNote(a) ?? a.detail ?? "no answer"})`).join("; ")}.
                </InlineNotice>
              )}
            </div>
          );
        }}
      </FleetGate>
    </Page>
  );
}

function FleetQualityPage() {
  return (
    <Page wide>
      <PageHeader title="Quality" description="The quality gate result for the build each agent is running." actions={<RefreshButton />} />
      <FleetGate title="Quality">
        {(data) => (
          <Card>
            <Table label="Quality gate by agent">
              <thead>
                <tr>
                  <Th>Agent</Th>
                  <Th>Gate</Th>
                  <Th className="text-right">Runs passing</Th>
                  <Th className="hidden text-right md:table-cell">Eval cases</Th>
                  <Th className="hidden md:table-cell">Kind</Th>
                  <Th className="hidden lg:table-cell">Run</Th>
                  <Th>
                    <span className="sr-only">Console</span>
                  </Th>
                </tr>
              </thead>
              <tbody>
                {data.agents.map((a) => {
                  const r = a.gate?.report;
                  return (
                    <Tr key={a.url}>
                      <Td className="max-w-[260px]">
                        <AgentName a={a} />
                      </Td>
                      <Td>
                        {r ? (
                          <StatusBadge tone={r.passed ? "ok" : "bad"}>{r.passed ? "Passed" : "Failed"}</StatusBadge>
                        ) : (
                          <span className="text-[13px] text-zinc-500">{a.gate?.error ?? (a.gate ? "No report in this build" : consoleNote(a) ?? "—")}</span>
                        )}
                      </Td>
                      <Td className="text-right tabular-nums">{r ? `${r.passed_runs} / ${r.runs}` : "—"}</Td>
                      <Td className="hidden text-right tabular-nums md:table-cell">{a.gate ? a.gate.cases : "—"}</Td>
                      <Td className="hidden md:table-cell">{r ? (r.live ? "Live" : "Offline") : "—"}</Td>
                      <Td className="hidden whitespace-nowrap lg:table-cell">{r ? timeAgo(r.started_at) : "—"}</Td>
                      <Td className="text-right">
                        <ConsoleLink a={a} />
                      </Td>
                    </Tr>
                  );
                })}
              </tbody>
            </Table>
          </Card>
        )}
      </FleetGate>
    </Page>
  );
}

const RANGES: { value: TrafficRange; label: string }[] = [
  { value: "1h", label: "Last hour" },
  { value: "24h", label: "24 hours" },
  { value: "7d", label: "7 days" },
];

function FleetTrafficPage() {
  const [range, setRange] = useState<TrafficRange>("24h");
  const traffic = useLoad(() => fleetApi.traffic(range), [range]);
  const fleet = useFleet();
  const names = useMemo(() => {
    const m = new Map<string, FleetAgent>();
    for (const a of fleet.data?.agents ?? []) if (a.overview?.agent.name) m.set(a.overview.agent.name, a);
    return m;
  }, [fleet.data]);
  const t: FleetTraffic | null = traffic.data;
  const rows = t?.agents ?? [];
  const max = Math.max(1, ...rows.map((r) => r.runs));
  return (
    <Page wide>
      <PageHeader title="Traffic" description="Runs, errors, latency and model tokens per agent, from Azure Monitor." />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Segmented label="Time range" value={range} options={RANGES} onChange={setRange} />
        <Button size="sm" variant="ghost" onClick={traffic.reload} disabled={traffic.loading}>
          <RotateCw aria-hidden className={cn(traffic.loading && "animate-spin motion-reduce:animate-none")} /> Refresh
        </Button>
        {t?.queried_at && <span className="text-xs text-zinc-500">Queried {timeAgo(t.queried_at)}</span>}
      </div>
      {traffic.error && !t ? (
        <Card>
          <ErrorState title="Unable to load traffic" message={traffic.error.message} onRetry={traffic.reload} />
        </Card>
      ) : !t ? (
        <LoadingRegion label="Loading traffic">
          <Skeleton className="h-64 rounded-lg" />
        </LoadingRegion>
      ) : !t.available ? (
        <Card>
          {t.error ? (
            <ErrorState title="Unable to load traffic" message={t.error} onRetry={traffic.reload} />
          ) : (
            <EmptyState icon={<Activity aria-hidden />} title="Traffic not connected">
              {t.reason}
            </EmptyState>
          )}
        </Card>
      ) : rows.length === 0 ? (
        <Card>
          <EmptyState icon={<Activity aria-hidden />} title="No agent traffic in this period" />
        </Card>
      ) : (
        <Card className={cn(traffic.loading && "opacity-60 transition-opacity")}>
          <Table label="Traffic by agent">
            <thead>
              <tr>
                <Th>Agent</Th>
                <Th className="w-[34%]">Requests</Th>
                <Th className="text-right">Errors</Th>
                <Th className="hidden text-right md:table-cell">Avg latency</Th>
                <Th className="text-right">Tokens</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const a = names.get(r.agent);
                return (
                  <Tr key={r.agent}>
                    <Td>
                      <div className="font-medium text-zinc-950">{a?.name ?? r.agent}</div>
                      <div className="text-xs text-zinc-500">
                        <span className="font-mono">{r.agent}</span>
                        {r.services.length > 1 && <> · reported by {r.services.length} services</>}
                        {!a && <Tag className="ml-1.5 text-2xs">not registered</Tag>}
                      </div>
                    </Td>
                    <Td>
                      <div className="flex items-center gap-2">
                        <div className="h-2 flex-1 rounded-full bg-zinc-100" aria-hidden>
                          <div className="h-2 rounded-full bg-accent-500" style={{ width: `${(r.runs / max) * 100}%` }} />
                        </div>
                        <span className="w-12 text-right tabular-nums text-zinc-900">{compact(r.runs)}</span>
                      </div>
                    </Td>
                    <Td className="text-right tabular-nums">
                      {compact(r.errors)}
                      {r.error_rate !== null && r.error_rate > 0 && (
                        <span className="ml-1 text-xs text-zinc-500">({(r.error_rate * 100).toFixed(1)}%)</span>
                      )}
                    </Td>
                    <Td className="hidden text-right tabular-nums md:table-cell">{r.avg_s === null ? "—" : duration(r.avg_s)}</Td>
                    <Td className="text-right tabular-nums">{compact(r.tokens)}</Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        </Card>
      )}
    </Page>
  );
}

// ------------------------------------------------------------------ app
function Routes() {
  const { path } = useRouter();
  useEffect(() => {
    const page = { "/": "Fleet", "/security": "Security", "/quality": "Quality", "/traffic": "Traffic" }[path] ?? "Not found";
    document.title = `${page} · Agent fleet · MAF Golden Path`;
  }, [path]);
  switch (path) {
    case "/":
      return <FleetOverviewPage />;
    case "/security":
      return <FleetSecurityPage />;
    case "/quality":
      return <FleetQualityPage />;
    case "/traffic":
      return <FleetTrafficPage />;
    default:
      return (
        <Page>
          <Card>
            <EmptyState title="Page not found">There's nothing at this address in the fleet view.</EmptyState>
          </Card>
        </Page>
      );
  }
}

export function FleetApp() {
  return (
    <RouterProvider>
      <FleetProvider>
        <FleetShell>
          <Routes />
        </FleetShell>
      </FleetProvider>
    </RouterProvider>
  );
}
