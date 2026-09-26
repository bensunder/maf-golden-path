import { Activity, ArrowUpRight, ClipboardCheck, MessageSquareText, RotateCw, Shield, UserCheck } from "lucide-react";
import { useMemo, type ReactNode } from "react";

import { EstimateNote, GoldenPathSummary, HealthDot, MoreLink, PostureList, postureSummary, useHealth } from "@/components/agent";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Page, PageHeader } from "@/components/ui/page";
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, type Tone } from "@/components/ui/status";
import { api, type Overview } from "@/lib/api";
import { useLoad, useOverview, useSessions } from "@/lib/data";
import { cn, humanize, shortId, timeAgo } from "@/lib/format";
import { Link, useRouter } from "@/lib/router";

export function OverviewPage() {
  const overview = useOverview();
  const { navigate } = useRouter();
  return (
    <Page>
      <PageHeader
        title="Overview"
        description="Monitor your governed AI agents, quality, security and production health."
        actions={
          <>
            <Button size="sm" onClick={overview.reload} aria-label="Refresh overview">
              <RotateCw aria-hidden /> Refresh
            </Button>
            <Button size="sm" variant="primary" onClick={() => navigate("/playground")}>
              <MessageSquareText aria-hidden /> Open playground
            </Button>
          </>
        }
      />
      {overview.error && !overview.data ? (
        <Card>
          <ErrorState title="Unable to load the overview" message={overview.error.message} onRetry={overview.reload} />
        </Card>
      ) : !overview.data ? (
        <OverviewSkeleton />
      ) : (
        <OverviewBody data={overview.data} />
      )}
    </Page>
  );
}

function OverviewSkeleton() {
  return (
    <LoadingRegion label="Loading overview" className="space-y-6">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <Card key={i} className="p-5">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="mt-4 h-7 w-16" />
            <Skeleton className="mt-3 h-3 w-28" />
          </Card>
        ))}
      </div>
      <div className="grid gap-6 xl:grid-cols-[1.4fr_1fr]">
        <Card className="h-64 p-5">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="mt-6 h-10 w-full" />
        </Card>
        <Card className="h-64 p-5">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="mt-6 h-40 w-full" />
        </Card>
      </div>
    </LoadingRegion>
  );
}

function StatCard({
  label,
  icon,
  value,
  caption,
  tone,
  status,
  to,
  loading,
}: {
  label: string;
  icon: ReactNode;
  value: ReactNode;
  caption: ReactNode;
  tone?: Tone;
  status?: string;
  to: string;
  loading?: boolean;
}) {
  return (
    <Link
      to={to}
      className="group block rounded-lg border border-zinc-200 bg-white p-5 shadow-card transition-colors hover:border-zinc-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
    >
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-2 text-[13px] font-medium text-zinc-500 [&_svg]:size-4 [&_svg]:text-zinc-500">
          {icon}
          {label}
        </span>
        <ArrowUpRight aria-hidden className="size-4 text-zinc-300 transition-colors group-hover:text-zinc-500" />
      </div>
      {loading ? (
        <>
          <Skeleton className="mt-4 h-7 w-16" />
          <Skeleton className="mt-3 h-3 w-28" />
        </>
      ) : (
        <>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-semibold tracking-tight text-zinc-950 tabular-nums">{value}</span>
            {tone && status && (
              <StatusBadge tone={tone} className="-translate-y-0.5">
                {status}
              </StatusBadge>
            )}
          </div>
          <p className="mt-1.5 truncate text-[13px] text-zinc-500">{caption}</p>
        </>
      )}
    </Link>
  );
}

function OverviewBody({ data }: { data: Overview }) {
  const { health } = useHealth();
  const evals = useLoad(api.evals);
  const { pendingCount, states, known } = useSessions();
  const posture = postureSummary(data.security);

  const evalCard = (() => {
    if (evals.error) return { value: "—", caption: "Couldn't load evaluations", tone: "warn" as Tone, status: "Unavailable" };
    if (evals.data?.report_error) return { value: "—", caption: evals.data.report_error, tone: "warn" as Tone, status: "Report unreadable" };
    if (evals.data?.cases_error) return { value: "—", caption: evals.data.cases_error };
    const report = evals.data?.report;
    if (report) {
      const passed = report.cases.reduce((n, c) => n + c.passed_runs, 0);
      const runs = report.cases.reduce((n, c) => n + c.runs, 0);
      return {
        value: `${passed} / ${runs}`,
        caption: `${report.live ? "Live" : "Offline"} gate run · ${timeAgo(report.started_at)}`,
        tone: (report.passed ? "ok" : "bad") as Tone,
        status: report.passed ? "Passed" : "Failed",
      };
    }
    const count = evals.data?.cases?.length;
    return { value: count ?? "—", caption: count ? "Cases defined · no gate report in this image" : "No eval cases found" };
  })();

  return (
    <div className="space-y-6">
      <section aria-label="Summary" className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Agents"
          icon={<Activity aria-hidden />}
          value={1}
          caption={`${data.agent.name} in this service`}
          tone={health?.tone}
          status={health?.label}
          to="/agents"
        />
        <StatCard label="Evaluations" icon={<ClipboardCheck aria-hidden />} {...evalCard} to="/evaluations" loading={evals.loading && !evals.data} />
        <StatCard
          label="Approvals"
          icon={<UserCheck aria-hidden />}
          value={pendingCount}
          tone={pendingCount ? "pending" : undefined}
          status={pendingCount ? "Pending" : undefined}
          caption={known.length ? "In your sessions" : "No sessions yet"}
          to="/approvals"
        />
        <StatCard
          label="Security"
          icon={<Shield aria-hidden />}
          value={`${posture.on}/${posture.total}`}
          tone={posture.tone}
          status={posture.label}
          caption="Controls on in the running agent"
          to="/security"
        />
      </section>

      <div className="grid gap-6 xl:grid-cols-[1.4fr_1fr]">
        <div className="space-y-6">
          <Card>
            <CardHeader title="Agent health" description="Agents served by this service." action={<MoreLink to="/agents">All agents</MoreLink>} />
            <ul>
              <li>
                <Link
                  to={`/agents/${encodeURIComponent(data.agent.name)}`}
                  className="flex flex-wrap items-center gap-x-6 gap-y-2 px-5 py-4 transition-colors hover:bg-zinc-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent-500"
                >
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-zinc-950">{data.service.title}</div>
                    <div className="mt-0.5 truncate text-[13px] text-zinc-500">{data.agent.description || data.agent.name}</div>
                  </div>
                  <span className="font-mono text-xs text-zinc-500">v{data.service.version}</span>
                  <span className="hidden text-[13px] text-zinc-500 sm:inline">{data.service.hosting.platform}</span>
                  <HealthDot health={health} />
                </Link>
              </li>
            </ul>
          </Card>

          <RecentActivity states={states} known={known} />
        </div>

        <Card>
          <CardHeader
            title="Security posture"
            description="From the agent's middleware stack and settings."
            icon={<Shield aria-hidden />}
            action={<MoreLink to="/security">Details</MoreLink>}
          />
          <CardBody className="py-1">
            <PostureList controls={data.security} compact />
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardHeader
          title="The golden path"
          description="The developer builds the agent. The platform team owns the enterprise plumbing."
          action={<MoreLink to="/platform">How it works</MoreLink>}
        />
        <CardBody className="grid gap-6 lg:grid-cols-[1fr_260px]">
          <GoldenPathSummary />
          <div className="rounded-lg border border-zinc-200 bg-zinc-50/60 px-5 py-4">
            <div className="text-2xs font-medium uppercase tracking-[0.06em] text-zinc-500">Estimated platform work saved</div>
            <div className="mt-1.5 text-xl font-semibold tracking-tight text-zinc-950">28–46 engineer-days</div>
            <div className="text-[13px] text-zinc-500">per agent team</div>
            <EstimateNote className="mt-3" />
          </div>
        </CardBody>
      </Card>
    </div>
  );
}

type Event = { at: number; title: string; detail: string; tone: Tone; key: string };

function RecentActivity({ states, known }: { states: ReturnType<typeof useSessions>["states"]; known: ReturnType<typeof useSessions>["known"] }) {
  const events = useMemo(() => {
    const list: Event[] = [];
    for (const s of known) {
      if (s.source === "playground")
        list.push({ at: s.created, title: "Conversation started", detail: `Session ${shortId(s.id)}`, tone: "neutral", key: `c-${s.id}` });
      const state = states[s.id];
      if (state?.status !== "ok") continue;
      for (const p of state.info.pending) {
        if (p.requested_at)
          list.push({ at: p.requested_at, title: "Approval requested", detail: `${humanize(p.tool)} · session ${shortId(s.id)}`, tone: "pending", key: `p-${p.id}` });
      }
      for (const a of state.info.audit) {
        list.push({
          at: a.decided_at,
          title: a.approved ? "Approval granted" : "Approval rejected",
          detail: `${humanize(a.tool)} · by ${a.decided_by_name || a.decided_by || "unknown"}`,
          tone: a.approved ? "ok" : "bad",
          key: `a-${a.id}`,
        });
      }
    }
    return list.sort((a, b) => b.at - a.at).slice(0, 8);
  }, [states, known]);

  return (
    <Card>
      <CardHeader title="Recent activity" description="Your sessions from this browser: conversations and approval decisions." />
      {events.length === 0 ? (
        <EmptyState compact icon={<Activity aria-hidden />} title="No activity yet" action={<MoreLink to="/playground">Start a conversation</MoreLink>}>
          Conversations you start in the playground and the approvals in them appear here. Service-wide activity is in the operations
          workbook.
        </EmptyState>
      ) : (
        <ol className="px-5 py-2">
          {events.map((e, i) => (
            <li key={e.key} className="relative flex gap-3 py-2.5">
              {i < events.length - 1 && <span aria-hidden className="absolute left-[3.5px] top-6 h-full w-px bg-zinc-200" />}
              <span
                aria-hidden
                className={cn(
                  "relative mt-1.5 size-2 shrink-0 rounded-full",
                  { ok: "bg-emerald-500", bad: "bg-red-500", pending: "bg-amber-500", warn: "bg-amber-500", info: "bg-accent-500", neutral: "bg-zinc-300" }[e.tone],
                )}
              />
              <div className="min-w-0 flex-1">
                <div className="text-sm text-zinc-900">{e.title}</div>
                <div className="truncate text-[13px] text-zinc-500">{e.detail}</div>
              </div>
              <time className="shrink-0 text-xs text-zinc-500" dateTime={new Date(e.at * 1000).toISOString()}>
                {timeAgo(e.at)}
              </time>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}
