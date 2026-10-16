// The run journal for a memory admin: what each recent run did (who or what started it, the outcome, tools,
// subjects). Never what was said: the journal holds no prompt or reply text, and people only as pseudonyms.

import { RefreshCw, ScrollText } from "lucide-react";
import { useCallback, useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, Tag } from "@/components/ui/status";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { OUTCOME_TONE, SUBJECT_PATTERN, TRIGGER_LABEL, type AgentMemoryClient, type JournalPage } from "@/lib/agentMemory";
import { dateTime, humanize, timeAgo } from "@/lib/format";

const RANGES = [
  { hours: 1, label: "Last hour" },
  { hours: 24, label: "Last 24 hours" },
  { hours: 72, label: "Last 3 days" },
  { hours: 168, label: "Last 7 days" },
];

function message(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong.";
}

export function JournalPanel({ client, retentionDays }: { client: AgentMemoryClient; retentionDays: number }) {
  const [hours, setHours] = useState(24);
  const [subject, setSubject] = useState("");
  const [page, setPage] = useState<JournalPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const rangeId = useId();
  const subjectId = useId();
  const filter = subject.trim();
  const valid = !filter || SUBJECT_PATTERN.test(filter);

  const load = useCallback(() => {
    if (!valid) return;
    let alive = true;
    setLoading(true);
    setError(null);
    client
      .journal(hours, filter || undefined)
      .then((p) => alive && setPage(p))
      .catch((err) => alive && setError(message(err)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [client, hours, filter, valid]);

  useEffect(() => load(), [load]);

  return (
    <div>
      <div className="flex flex-col gap-3 border-b border-zinc-100 px-5 py-3 sm:flex-row sm:items-end">
        <div>
          <label htmlFor={rangeId} className="block text-2xs font-medium text-zinc-600">
            Period
          </label>
          <select
            id={rangeId}
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
            className="mt-1 h-8 rounded-md border border-zinc-300 bg-white px-2 text-[13px] focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/30"
          >
            {RANGES.map((r) => (
              <option key={r.hours} value={r.hours}>
                {r.label}
              </option>
            ))}
          </select>
        </div>
        <div className="min-w-0 flex-1">
          <label htmlFor={subjectId} className="block text-2xs font-medium text-zinc-600">
            Subject (optional)
          </label>
          <input
            id={subjectId}
            value={subject}
            placeholder="customer:1042"
            aria-invalid={!valid || undefined}
            onChange={(e) => setSubject(e.target.value)}
            className="mt-1 h-8 w-full rounded-md border border-zinc-300 px-2.5 font-mono text-[13px] focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/30 sm:max-w-xs"
          />
        </div>
        <Button size="sm" variant="secondary" onClick={load} disabled={loading || !valid}>
          <RefreshCw aria-hidden /> Refresh
        </Button>
      </div>
      {!valid && (
        <div className="px-5 pt-4">
          <InlineNotice tone="warn">Use type:id, e.g. customer:1042.</InlineNotice>
        </div>
      )}
      {page && page.dropped > 0 && (
        <div className="px-5 pt-4">
          <InlineNotice tone="warn">
            {page.dropped} run{page.dropped === 1 ? " was" : "s were"} not recorded in this period because an hour's journal page was full.
          </InlineNotice>
        </div>
      )}
      {error && !page ? (
        <ErrorState compact title="Unable to load the journal" message={error} onRetry={load} />
      ) : !page ? (
        <LoadingRegion label="Loading the journal" className="space-y-3 p-5">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </LoadingRegion>
      ) : page.entries.length ? (
        <Table label="Recent runs">
          <thead>
            <tr>
              <Th>When</Th>
              <Th>Started by</Th>
              <Th>Outcome</Th>
              <Th className="hidden md:table-cell">Tools</Th>
              <Th className="hidden lg:table-cell">Subjects</Th>
            </tr>
          </thead>
          <tbody>
            {page.entries.map((e) => (
              <Tr key={e.run_id}>
                <Td className="whitespace-nowrap">
                  <time title={dateTime(e.at)}>{timeAgo(e.at)}</time>
                  <div className="text-2xs text-zinc-500">{(e.duration_ms / 1000).toFixed(1)} s</div>
                </Td>
                <Td className="whitespace-nowrap">
                  {TRIGGER_LABEL[e.trigger]}
                  {e.actor && <div className="font-mono text-2xs text-zinc-500">{e.actor}</div>}
                </Td>
                <Td className="whitespace-nowrap">
                  <StatusBadge tone={OUTCOME_TONE[e.outcome]}>{humanize(e.outcome)}</StatusBadge>
                  {e.blocked_reason && <div className="text-2xs text-zinc-500">{humanize(e.blocked_reason)}</div>}
                  {e.lessons_proposed > 0 && (
                    <div className="text-2xs text-zinc-500">
                      {e.lessons_proposed} lesson{e.lessons_proposed === 1 ? "" : "s"} proposed
                    </div>
                  )}
                </Td>
                <Td className="hidden md:table-cell">
                  <div className="flex flex-wrap gap-1">
                    {e.tools.length ? e.tools.map((t) => <Tag key={t} mono>{t}</Tag>) : <span className="text-zinc-400">none</span>}
                  </div>
                </Td>
                <Td className="hidden lg:table-cell">
                  <div className="flex flex-wrap gap-1">
                    {e.subjects.map((s) => (
                      <Tag key={s} mono>
                        {s}
                      </Tag>
                    ))}
                  </div>
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      ) : (
        <EmptyState compact icon={<ScrollText aria-hidden />} title="No runs recorded in this period">
          Every run is recorded when it finishes, without its text. Kept {retentionDays} days.
        </EmptyState>
      )}
    </div>
  );
}
