// Subject memory for a memory admin: which customers, invoices or cases have history, what happened to
// each, and erasing one (an erasure request, or a mistake). Ids only: names and contact details are refused.

import { FolderSearch, Search, Trash2 } from "lucide-react";
import { useCallback, useEffect, useId, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { CardBody } from "@/components/ui/card";
import { Dialog } from "@/components/ui/overlay";
import { EmptyState, ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { Tag } from "@/components/ui/status";
import { SUBJECT_PATTERN, TRIGGER_LABEL, humanizeEvent, type AgentMemoryClient, type SubjectEvent, type SubjectRef } from "@/lib/agentMemory";
import { cn, dateTime, timeAgo } from "@/lib/format";

function message(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong.";
}

export function SubjectsPanel({
  client,
  retentionDays,
  announce,
}: {
  client: AgentMemoryClient;
  retentionDays: number;
  announce: (text: string) => void;
}) {
  const [subjects, setSubjects] = useState<SubjectRef[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(() => {
    let alive = true;
    setError(null);
    client
      .subjects()
      .then((r) => alive && setSubjects(r.items))
      .catch((err) => alive && setError(message(err)));
    return () => {
      alive = false;
    };
  }, [client]);

  useEffect(() => load(), [load]);

  if (error && !subjects) return <ErrorState compact title="Unable to load subjects" message={error} onRetry={load} />;
  if (!subjects) {
    return (
      <LoadingRegion label="Loading subjects" className="space-y-3 p-5">
        <Skeleton className="h-4 w-1/3" />
        <Skeleton className="h-4 w-1/2" />
      </LoadingRegion>
    );
  }

  return (
    <div className="grid md:grid-cols-[minmax(0,16rem)_minmax(0,1fr)]">
      <div className="border-b border-zinc-100 md:border-b-0 md:border-r">
        <CardBody className="border-b border-zinc-100">
          <FindSubject onFind={setSelected} />
        </CardBody>
        {subjects.length ? (
          <ul aria-label="Subjects with history" className="max-h-[28rem] overflow-y-auto py-1">
            {subjects.map((s) => (
              <li key={s.subject}>
                <button
                  type="button"
                  aria-current={selected === s.subject || undefined}
                  onClick={() => setSelected(s.subject)}
                  className={cn(
                    "flex w-full items-center justify-between gap-2 px-5 py-2 text-left text-[13px] hover:bg-zinc-50",
                    selected === s.subject && "bg-zinc-100",
                  )}
                >
                  <span className="truncate font-mono text-zinc-900">{s.subject}</span>
                  <time className="shrink-0 text-2xs text-zinc-500" title={dateTime(s.last_at)}>
                    {timeAgo(s.last_at)}
                  </time>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState compact icon={<FolderSearch aria-hidden />} title="No subject history yet">
            When the agent records what it did for a customer or invoice, the subject appears here. Kept {retentionDays} days.
          </EmptyState>
        )}
      </div>
      <div className="min-w-0">
        {selected ? (
          <SubjectDetail
            key={selected}
            client={client}
            subject={selected}
            onForgotten={() => {
              announce(`History of ${selected} erased.`);
              setSelected(null);
              load();
            }}
          />
        ) : (
          <EmptyState compact title="Choose a subject">See everything the agent recorded for it, oldest first.</EmptyState>
        )}
      </div>
    </div>
  );
}

function FindSubject({ onFind }: { onFind: (subject: string) => void }) {
  const [value, setValue] = useState("");
  const [invalid, setInvalid] = useState(false);
  const id = useId();
  const hint = useId();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const raw = value.trim();
    const [kind, ...rest] = raw.split(":");
    const subject = rest.length ? `${kind.toLowerCase()}:${rest.join(":")}` : raw;
    if (!SUBJECT_PATTERN.test(subject)) return setInvalid(true);
    setInvalid(false);
    onFind(subject);
  };
  return (
    <form onSubmit={submit} className="space-y-1.5">
      <label htmlFor={id} className="block text-[13px] font-medium text-zinc-800">
        Find a subject
      </label>
      <div className="flex gap-2">
        <input
          id={id}
          value={value}
          placeholder="customer:1042"
          aria-invalid={invalid || undefined}
          aria-describedby={hint}
          onChange={(e) => setValue(e.target.value)}
          className="h-8 min-w-0 flex-1 rounded-md border border-zinc-300 px-2.5 font-mono text-[13px] focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/30"
        />
        <Button type="submit" size="icon" variant="secondary" aria-label="Find">
          <Search aria-hidden />
        </Button>
      </div>
      <p id={hint} className={cn("text-2xs", invalid ? "text-red-700" : "text-zinc-500")}>
        {invalid ? "Use type:id, e.g. customer:1042 or invoice:INV-0042." : "type:id"}
      </p>
    </form>
  );
}

function SubjectDetail({ client, subject, onForgotten }: { client: AgentMemoryClient; subject: string; onForgotten: () => void }) {
  const [events, setEvents] = useState<SubjectEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    let alive = true;
    setError(null);
    client
      .subject(subject)
      .then((r) => alive && setEvents(r.events))
      .catch((err) => alive && setError(message(err)));
    return () => {
      alive = false;
    };
  }, [client, subject]);

  useEffect(() => load(), [load]);

  const forget = async () => {
    setBusy(true);
    try {
      await client.forgetSubject(subject);
      setConfirming(false);
      onForgotten();
    } catch (err) {
      setError(message(err));
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-100 px-5 py-3">
        <h3 className="font-mono text-[13px] font-semibold text-zinc-900">{subject}</h3>
        <Button size="sm" variant="danger-outline" disabled={!events?.length} onClick={() => setConfirming(true)}>
          <Trash2 aria-hidden /> Erase history
        </Button>
      </div>
      {error && (
        <div className="px-5 pt-4">
          <InlineNotice tone="bad">{error}</InlineNotice>
        </div>
      )}
      {!events ? (
        !error && (
          <LoadingRegion label="Loading history" className="space-y-3 p-5">
            <Skeleton className="h-4 w-2/3" />
          </LoadingRegion>
        )
      ) : events.length ? (
        <ol className="divide-y divide-zinc-100" aria-label={`History of ${subject}, oldest first`}>
          {events.map((e) => (
            <li key={e.id} className="px-5 py-2.5 text-[13px]">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-medium text-zinc-900">{humanizeEvent(e.event)}</span>
                <time className="text-2xs text-zinc-500" title={dateTime(e.at)}>
                  {dateTime(e.at)}
                </time>
              </div>
              {e.note && <p className="mt-0.5 break-words text-zinc-600">{e.note}</p>}
              <div className="mt-1 flex flex-wrap gap-1.5">
                <Tag>{TRIGGER_LABEL[e.trigger]}</Tag>
                {e.run_id && (
                  <Tag mono className="max-w-[14rem] truncate">
                    run {e.run_id}
                  </Tag>
                )}
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <EmptyState compact title="No recorded history">Nothing is remembered for {subject}.</EmptyState>
      )}
      <Dialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Erase the history of ${subject}?`}
        description="The agent will no longer know what happened to it. This can't be undone."
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button variant="danger" disabled={busy} onClick={forget}>
              {busy ? "Erasing…" : "Erase"}
            </Button>
          </>
        }
      />
    </div>
  );
}
