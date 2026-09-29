import { ArrowRight, Network } from "lucide-react";

import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Page, PageHeader } from "@/components/ui/page";
import { EmptyState, ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, Tag, type Tone } from "@/components/ui/status";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { networkApi, type AgentCall, type AgentNetwork } from "@/lib/api";
import { useLoad } from "@/lib/data";
import { timeAgo } from "@/lib/format";
import { Link } from "@/lib/router";

const OUTCOME: Record<AgentCall["outcome"], { tone: Tone; label: string }> = {
  answered: { tone: "ok", label: "Answered" },
  approval_requested: { tone: "pending", label: "Asked for approval" },
  approved: { tone: "ok", label: "Approved and done" },
  rejected: { tone: "neutral", label: "Rejected" },
  blocked: { tone: "warn", label: "Declined by its guardrails" },
  refused: { tone: "bad", label: "Refused by the platform" },
  error: { tone: "bad", label: "Failed" },
};

/** Agents on a circle, arrows for "may call". Small networks read well this way; the table below has the detail. */
function Graph({ network }: { network: AgentNetwork }) {
  const n = network.agents.length;
  const size = 360;
  const r = n > 1 ? 130 : 0;
  const at = (i: number) => ({ x: size / 2 + r * Math.cos((2 * Math.PI * i) / n - Math.PI / 2), y: size / 2 + r * Math.sin((2 * Math.PI * i) / n - Math.PI / 2) });
  const pos = Object.fromEntries(network.agents.map((a, i) => [a.name, at(i)]));
  return (
    <svg viewBox={`0 0 ${size} ${size}`} role="img" aria-label="Which agents may call which" className="mx-auto h-auto w-full max-w-[420px]">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" className="fill-accent-600" />
        </marker>
      </defs>
      {network.edges.map((e) => {
        const a = pos[e.from];
        const b = pos[e.to];
        if (!a || !b) return null;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const len = Math.hypot(dx, dy) || 1;
        const pad = 28;
        return (
          <line key={`${e.from}>${e.to}`} x1={a.x + (dx / len) * pad} y1={a.y + (dy / len) * pad} x2={b.x - (dx / len) * pad} y2={b.y - (dy / len) * pad}
            className="stroke-accent-600" strokeWidth={1.75} markerEnd="url(#arrow)" />
        );
      })}
      {network.agents.map((a) => {
        const p = pos[a.name];
        const label = a.title.length > 24 ? a.title.slice(0, 23) + "…" : a.title;
        return (
          <g key={a.name}>
            <title>{a.title}</title>
            <circle cx={p.x} cy={p.y} r={22} className="fill-white stroke-zinc-300" strokeWidth={1.5} />
            <text x={p.x} y={p.y + 4} textAnchor="middle" className="fill-zinc-700 text-[11px] font-semibold">
              {a.title.slice(0, 1).toUpperCase()}
            </text>
            {/* outside the circle, away from the centre where the arrows run */}
            <text x={p.x} y={p.y < size / 2 - 1 ? p.y - 30 : p.y + 38} textAnchor="middle" className="fill-zinc-900 text-[11px]">{label}</text>
          </g>
        );
      })}
    </svg>
  );
}

export function NetworkPage() {
  const { data, error, reload } = useLoad(networkApi.get);
  const title = (name: string) => data?.agents.find((a) => a.name === name)?.title ?? name;
  return (
    <Page>
      <PageHeader
        title="Agent network"
        description="Which agents may call which, and the calls they made. Every call acts for the signed-in person who started it."
      />
      {error && !data ? (
        <ErrorState title="Unable to load the agent network" message={error.message} onRetry={reload} />
      ) : !data ? (
        <LoadingRegion label="Loading" className="space-y-3"><Skeleton className="h-4 w-1/3" /></LoadingRegion>
      ) : (
        <div className="space-y-6">
          <div className="grid gap-6 xl:grid-cols-[1fr_380px]">
            <Card>
              <CardHeader title="Who may call whom" description="Set on the Agents page (Can call), by a platform admin." icon={<Network aria-hidden />} />
              <CardBody>
                {data.edges.length === 0 ? (
                  <EmptyState title="No agent calls another yet">On the <Link to="/agents" className="font-medium text-accent-700 hover:underline">Agents</Link> page, use Can call to let one agent ask another.</EmptyState>
                ) : (
                  <Graph network={data} />
                )}
              </CardBody>
            </Card>
            <div className="space-y-4">
              <Card className="p-5">
                <div className="text-2xs font-medium uppercase tracking-[0.06em] text-zinc-500">Every call is checked</div>
                <ul className="mt-3 space-y-2 text-[13px] leading-relaxed text-zinc-600">
                  <li>Acts for the person who started the request, from a delegation the platform signed. No agent can pick someone else.</li>
                  <li>Only where an admin allowed it. Never loops back; at most {data.limits.depth} agents deep and {data.limits.calls_per_request} calls per request.</li>
                  <li>The other agent runs its own guardrails and approvals. Its approvals come back to the person, and go ahead only if what they approved matches.</li>
                  <li>Every agent only accepts requests the platform signed for it.</li>
                </ul>
              </Card>
              {data.edges.length > 0 && (
                <Card className="p-5">
                  <div className="text-2xs font-medium uppercase tracking-[0.06em] text-zinc-500">Allowed calls</div>
                  <ul className="mt-2 space-y-1.5 text-[13px] text-zinc-700">
                    {data.edges.map((e) => (
                      <li key={`${e.from}>${e.to}`} className="flex items-center gap-1.5">
                        {title(e.from)} <ArrowRight aria-hidden className="size-3.5 text-zinc-400" /> {title(e.to)}
                      </li>
                    ))}
                  </ul>
                </Card>
              )}
            </div>
          </div>
          <Card>
            <CardHeader title="Recent calls" description={data.all_calls ? "Every agent-to-agent call on this server (admins see everyone's)." : "Calls made for you."} />
            {data.calls.length === 0 ? (
              <CardBody><p className="text-[13px] text-zinc-500">No calls yet.</p></CardBody>
            ) : (
              <Table label="Recent agent-to-agent calls">
                <thead>
                  <tr>
                    <Th>When</Th>
                    <Th>Chain</Th>
                    {data.all_calls && <Th className="hidden md:table-cell">For</Th>}
                    <Th>Outcome</Th>
                    <Th className="hidden lg:table-cell">Detail</Th>
                    <Th className="hidden md:table-cell text-right">Time</Th>
                  </tr>
                </thead>
                <tbody>
                  {data.calls.map((c, i) => (
                    <Tr key={`${c.at}-${i}`}>
                      <Td className="whitespace-nowrap text-[13px] text-zinc-600">{timeAgo(c.at)}</Td>
                      <Td>
                        <div className="flex flex-wrap items-center gap-1">
                          {c.chain.map((a, j) => (
                            <span key={`${a}-${j}`} className="inline-flex items-center gap-1">
                              {j > 0 && <ArrowRight aria-hidden className="size-3 text-zinc-400" />}
                              <Tag>{title(a)}</Tag>
                            </span>
                          ))}
                        </div>
                      </Td>
                      {data.all_calls && <Td className="hidden md:table-cell text-[13px] text-zinc-600">{c.user ?? "—"}</Td>}
                      <Td><StatusBadge tone={OUTCOME[c.outcome]?.tone ?? "neutral"}>{OUTCOME[c.outcome]?.label ?? c.outcome}</StatusBadge></Td>
                      <Td className="hidden lg:table-cell text-[13px] text-zinc-600">{c.detail ?? ""}</Td>
                      <Td className="hidden md:table-cell text-right text-[13px] tabular-nums text-zinc-600">{c.ms != null ? `${(c.ms / 1000).toFixed(1)} s` : ""}</Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>
          <InlineNotice>
            Each call is also a span in the agents' traces (Application Insights, OTLP or LangSmith), one trace per person's request
            across every agent it reached.
          </InlineNotice>
        </div>
      )}
    </Page>
  );
}
