// Building blocks shared by Overview, Agent, Security and Playground. All read the live overview.
import { ArrowRight, CircleCheck, CircleMinus, Info, TriangleAlert } from "lucide-react";
import { useEffect, type ReactNode } from "react";

import { api, type Control, type Overview, type ToolInfo } from "@/lib/api";
import { useLoad } from "@/lib/data";
import { cn, humanize } from "@/lib/format";
import { Link } from "@/lib/router";

import { Skeleton } from "./ui/states";
import { StatusBadge, StatusDot, Tag, type Tone } from "./ui/status";

// ------------------------------------------------------------------ health
export type Health = { tone: Tone; label: string; detail: string };

/** /readyz, checked every 30 s. "Ready" means the service answers and its agent is loaded; it doesn't
 *  probe the model gateway, the session store or search (their failures show up as run errors). */
export function useHealth() {
  const ready = useLoad(api.ready);
  const { reload } = ready;
  useEffect(() => {
    const timer = window.setInterval(() => document.visibilityState === "visible" && reload(), 30_000);
    return () => window.clearInterval(timer);
  }, [reload]);
  let health: Health | null = null;
  if (ready.error) health = { tone: "bad", label: "Unavailable", detail: ready.error.message };
  else if (ready.data)
    health = { tone: "ok", label: "Ready", detail: "The service answers and the agent is loaded (/readyz). Model, store and search aren't probed." };
  return { health, loading: ready.loading && !ready.data, reload };
}

export function HealthDot({ health }: { health: Health | null }) {
  if (!health) return <Skeleton className="h-4 w-16" />;
  return (
    <span title={health.detail}>
      <StatusDot tone={health.tone}>{health.label}</StatusDot>
    </span>
  );
}

// ------------------------------------------------------------------ security posture
const CONTROL_TONE: Record<Control["status"], Tone> = { on: "ok", partial: "warn", off: "neutral" };
const CONTROL_TEXT: Record<Control["status"], string> = { on: "On", partial: "Partial", off: "Off" };

export function postureSummary(controls: Control[]) {
  const on = controls.filter((c) => c.status === "on").length;
  const partial = controls.filter((c) => c.status === "partial").length;
  const off = controls.filter((c) => c.status === "off").length;
  const gaps = off + partial;
  const tone: Tone = gaps === 0 ? "ok" : "warn";
  const label = gaps === 0 ? "All controls on" : `${gaps} not fully on`;
  return { on, partial, off, total: controls.length, tone, label };
}

function ControlIcon({ status }: { status: Control["status"] }) {
  if (status === "on") return <CircleCheck aria-hidden className="size-[18px] text-emerald-600" />;
  if (status === "partial") return <TriangleAlert aria-hidden className="size-[18px] text-amber-600" />;
  return <CircleMinus aria-hidden className="size-[18px] text-zinc-500" />;
}

/** The posture list. ``compact`` hides details (they stay in the title attribute). */
export function PostureList({ controls, compact, className }: { controls: Control[]; compact?: boolean; className?: string }) {
  return (
    <ul className={cn("divide-y divide-zinc-100", className)} aria-label="Security controls">
      {controls.map((c) => (
        <li key={c.id} className={cn("flex items-start gap-3", compact ? "py-2" : "py-3")} title={compact ? c.detail : undefined}>
          <span className="mt-px">
            <ControlIcon status={c.status} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-3">
              <span className={cn("text-sm", c.status === "off" ? "text-zinc-500" : "text-zinc-900")}>{c.name}</span>
              <StatusBadge tone={CONTROL_TONE[c.status]} icon={false} className="text-2xs">
                {CONTROL_TEXT[c.status]}
              </StatusBadge>
            </div>
            {!compact && <p className="mt-0.5 text-[13px] leading-relaxed text-zinc-500">{c.detail}</p>}
          </div>
        </li>
      ))}
    </ul>
  );
}

// ------------------------------------------------------------------ runtime
export function approvalLabel(tool: ToolInfo): string {
  return tool.approval === "always" ? "Always needs approval" : tool.approval === "rules" ? "Approval by rule" : "No approval";
}

export function RuntimeGrid({ data }: { data: Overview }) {
  const { agent, knowledge, channels } = data;
  const extraChannels = channels.filter((c) => c.id !== "api" && c.id !== "agui");
  return (
    <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-zinc-200 bg-zinc-200 md:grid-cols-4">
      <RuntimeCell label="Model">
        <span className="font-mono text-[13px]">{agent.model}</span>
        <Sub>{agent.gateway ? "Through the AI gateway" : "Direct (no gateway configured)"}</Sub>
      </RuntimeCell>
      <RuntimeCell label="Tools">
        {agent.tools.length} active
        <Sub>
          {agent.tools.filter((t) => t.approval !== "never").length
            ? `${agent.tools.filter((t) => t.approval !== "never").length} with human approval`
            : "None need approval"}
        </Sub>
      </RuntimeCell>
      <RuntimeCell label="Knowledge">
        {knowledge ? "Azure AI Search" : <span className="text-zinc-500">Not configured</span>}
        <Sub>{knowledge ? <span className="font-mono">{knowledge.index}</span> : "No knowledge tool"}</Sub>
      </RuntimeCell>
      <RuntimeCell label="Channels">
        {extraChannels.length ? extraChannels.map((c) => c.name).join(", ") : "JSON API"}
        <Sub>{channels.length} endpoint{channels.length === 1 ? "" : "s"} incl. JSON API</Sub>
      </RuntimeCell>
    </dl>
  );
}

function RuntimeCell({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="bg-white px-4 py-3.5">
      <dt className="text-2xs font-medium uppercase tracking-[0.06em] text-zinc-500">{label}</dt>
      <dd className="mt-1.5 text-sm font-medium text-zinc-900">{children}</dd>
    </div>
  );
}

function Sub({ children }: { children: ReactNode }) {
  return <div className="mt-0.5 truncate text-xs font-normal text-zinc-500">{children}</div>;
}

export function ToolList({ tools }: { tools: ToolInfo[] }) {
  return (
    <ul className="divide-y divide-zinc-100">
      {tools.map((t) => (
        <li key={t.name} className="flex items-start gap-3 px-5 py-3">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-[13px] text-zinc-900">{t.name}</span>
              {t.kind === "knowledge" && <Tag>Knowledge</Tag>}
            </div>
            {t.description && <p className="mt-0.5 line-clamp-2 text-[13px] text-zinc-500">{t.description}</p>}
          </div>
          {t.approval !== "never" ? (
            <StatusBadge tone="pending" className="mt-0.5">
              {t.approval === "rules" ? "Approval by rule" : "Approval"}
            </StatusBadge>
          ) : (
            <span className="mt-0.5 text-xs text-zinc-500">Runs directly</span>
          )}
        </li>
      ))}
    </ul>
  );
}

// ------------------------------------------------------------------ golden path
const DEVELOPER_WRITES = ["Tools", "Instructions", "Business logic", "Evaluation cases"];
const PLATFORM_PROVIDES = [
  "Authentication",
  "Guardrails",
  "Sessions",
  "Approvals",
  "Telemetry",
  "Knowledge",
  "Testing",
  "Infrastructure",
  "CI/CD",
  "Channels",
];

export function GoldenPathSummary({ compact }: { compact?: boolean }) {
  return (
    <div className={cn("grid gap-px overflow-hidden rounded-lg border border-zinc-200 bg-zinc-200", compact ? "grid-cols-1" : "md:grid-cols-[1fr_1.4fr]")}>
      <div className="bg-white px-5 py-4">
        <div className="text-2xs font-medium uppercase tracking-[0.06em] text-zinc-500">Your team writes</div>
        <ul className="mt-2.5 flex flex-wrap gap-1.5">
          {DEVELOPER_WRITES.map((w) => (
            <li key={w} className="rounded-md border border-accent-200 bg-accent-50 px-2 py-1 text-[13px] font-medium text-accent-800">
              {w}
            </li>
          ))}
        </ul>
      </div>
      <div className="bg-zinc-50 px-5 py-4">
        <div className="text-2xs font-medium uppercase tracking-[0.06em] text-zinc-500">The platform provides</div>
        <ul className="mt-2.5 flex flex-wrap gap-1.5">
          {PLATFORM_PROVIDES.map((w) => (
            <li key={w} className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-[13px] text-zinc-700">
              {w}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

export function EstimateNote({ className }: { className?: string }) {
  return (
    <p className={cn("flex items-start gap-2 text-xs leading-relaxed text-zinc-500", className)}>
      <Info aria-hidden className="mt-0.5 size-3.5 shrink-0" />
      <span>
        Estimate based on the engineering effort documented in <span className="font-mono">docs/why-agentkit.md</span>, plus 1–3 days per
        downstream API. This is not a controlled benchmark measurement.
      </span>
    </p>
  );
}

export function MoreLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link
      to={to}
      className="inline-flex items-center gap-1 rounded text-[13px] font-medium text-accent-700 hover:text-accent-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
    >
      {children}
      <ArrowRight aria-hidden className="size-3.5" />
    </Link>
  );
}

export { humanize };
