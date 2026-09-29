import { ExternalLink, Landmark } from "lucide-react";
import { useState } from "react";

import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { StatusBadge, Tag, type Tone } from "@/components/ui/status";
import type { Control } from "@/lib/api";
import { cn } from "@/lib/format";
import { FRAMEWORKS, coverage, coverageSummary, type CoverageRow, type FrameworkId } from "@/lib/nist";

const TONE: Record<CoverageRow["status"], Tone> = { on: "ok", partial: "warn", off: "bad", none: "neutral" };
const TEXT: Record<CoverageRow["status"], string> = { on: "Supported", partial: "Partly", off: "Not in force", none: "No evidence here" };

/** NIST references with this agent's live controls as the evidence (a mapping to start an assessment from). */
export function FrameworkCoverage({ controls }: { controls: Control[] }) {
  const [active, setActive] = useState<FrameworkId>("ai_rmf");
  const framework = FRAMEWORKS.find((f) => f.id === active)!;
  const rows = coverage(active, controls);
  const summary = coverageSummary(rows);
  return (
    <Card>
      <CardHeader
        title="NIST frameworks"
        description="Which NIST references this agent's running controls support, with the controls as evidence."
        icon={<Landmark aria-hidden />}
      />
      <div role="tablist" aria-label="Framework" className="flex flex-wrap gap-1.5 border-b border-zinc-100 px-5 pb-3">
        {FRAMEWORKS.map((f) => (
          <button
            key={f.id}
            role="tab"
            type="button"
            aria-selected={f.id === active}
            onClick={() => setActive(f.id)}
            className={cn(
              "rounded-md border px-2.5 py-1 text-[13px]",
              f.id === active ? "border-zinc-900 bg-zinc-900 text-white" : "border-zinc-200 text-zinc-700 hover:border-zinc-300",
            )}
          >
            {f.short}
          </button>
        ))}
      </div>
      <CardBody className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-[13px] text-zinc-600">
          <a href={framework.url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 font-medium text-accent-700 hover:underline">
            {framework.title} <ExternalLink aria-hidden className="size-3" />
          </a>
          <span>
            <span className="font-medium text-zinc-900">{summary.on}</span> supported · {summary.partial} partly · {summary.off} not in force
            {summary.none ? ` · ${summary.none} with no evidence here` : ""}
          </span>
        </div>
        <ul className="divide-y divide-zinc-100" aria-label={`${framework.short} references`}>
          {rows.map((r) => (
            <li key={r.ref} className="py-2.5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <span className="font-mono text-[12.5px] font-medium text-zinc-900">{r.ref}</span>
                  <span className="ml-2 text-[13px] text-zinc-600">{r.title}</span>
                </div>
                <StatusBadge tone={TONE[r.status]} icon={false} className="shrink-0 text-2xs">
                  {TEXT[r.status]}
                </StatusBadge>
              </div>
              {r.evidence.length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {r.evidence.map((c) => (
                    <span key={c.id} title={c.detail}>
                      <Tag className={c.status === "on" ? "" : c.status === "partial" ? "border-amber-200 bg-amber-50" : "border-red-200 bg-red-50"}>
                        {c.name}
                      </Tag>
                    </span>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
        <p className="text-xs leading-relaxed text-zinc-500">
          Statuses are read from the running agent: its middleware stack, settings and latest quality gate. The mapping to NIST
          references is agentkit's, for an assessor to review; it is not a certification or an assessment, and organizational
          controls (policies, training, incident response) are outside what an agent can show.
        </p>
      </CardBody>
    </Card>
  );
}
