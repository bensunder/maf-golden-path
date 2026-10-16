// The agent's lessons for a memory admin: the review queue first, then what's in use, then the archive.
// Each lesson shows where it came from and its history; an admin approves, rejects, edits, rolls back,
// archives or restores it. Every change waits for the service (no optimistic state: these are reviews).

import { Archive, Check, History, Pencil, Plus, RotateCcw, Undo2, X } from "lucide-react";
import { useCallback, useEffect, useId, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { CardBody } from "@/components/ui/card";
import { EmptyState, ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, Tag } from "@/components/ui/status";
import {
  ORIGIN_LABEL,
  lessonsIn,
  revertOptions,
  upsertLesson,
  type AgentMemoryClient,
  type Lesson,
  type LessonStatus,
} from "@/lib/agentMemory";
import { cn, dateTime, timeAgo, timeUntil } from "@/lib/format";

const TABS: { id: LessonStatus; label: string }[] = [
  { id: "pending", label: "Waiting for review" },
  { id: "active", label: "In use" },
  { id: "archived", label: "Archived" },
];

function message(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong.";
}

export function LessonsPanel({
  client,
  maxChars,
  reviewDays,
  announce,
}: {
  client: AgentMemoryClient;
  maxChars: number;
  reviewDays: number;
  announce: (text: string) => void;
}) {
  const [items, setItems] = useState<Lesson[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [tab, setTab] = useState<LessonStatus>("pending");
  const tabsId = useId();

  const load = useCallback(() => {
    let alive = true;
    setError(null);
    client
      .lessons()
      .then((list) => {
        if (!alive) return;
        setItems(list.items);
        // Open on the queue when something waits; otherwise on what's in use.
        setTab((current) => (current === "pending" && !list.counts.pending ? "active" : current));
      })
      .catch((err) => alive && setError(message(err)));
    return () => {
      alive = false;
    };
  }, [client]);

  useEffect(() => load(), [load]);

  const act = async (id: string, run: () => Promise<Lesson | void>, done: string) => {
    setBusy(id);
    setNotice(null);
    try {
      const lesson = await run();
      if (lesson) setItems((list) => upsertLesson(list ?? [], lesson));
      else load();
      announce(done);
    } catch (err) {
      setNotice(message(err));
      announce(`Not done. ${message(err)}`);
    } finally {
      setBusy(null);
    }
  };

  if (error && !items) return <ErrorState compact title="Unable to load lessons" message={error} onRetry={load} />;
  if (!items) {
    return (
      <LoadingRegion label="Loading lessons" className="space-y-3 p-5">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
      </LoadingRegion>
    );
  }

  const shown = lessonsIn(items, tab);
  const count = (s: LessonStatus) => items.filter((l) => l.status === s).length;

  return (
    <div>
      <CardBody className="border-b border-zinc-100">
        <AddLessonForm
          maxChars={maxChars}
          onAdd={async (text) => {
            try {
              const lesson = await client.addLesson(text);
              setItems((list) => upsertLesson(list ?? [], lesson));
              setTab("active");
              announce("Lesson saved and in use.");
              setNotice(null);
              return true;
            } catch (err) {
              setNotice(message(err));
              announce(`Not saved. ${message(err)}`);
              return false;
            }
          }}
        />
      </CardBody>
      <div role="tablist" aria-label="Lessons" className="flex flex-wrap gap-1.5 border-b border-zinc-100 px-5 py-3">
        {TABS.map((t) => (
          <button
            key={t.id}
            id={`${tabsId}-${t.id}`}
            role="tab"
            type="button"
            aria-selected={t.id === tab}
            aria-controls={`${tabsId}-panel`}
            onClick={() => setTab(t.id)}
            className={cn(
              "rounded-md border px-2.5 py-1 text-[13px]",
              t.id === tab ? "border-zinc-900 bg-zinc-900 text-white" : "border-zinc-200 text-zinc-700 hover:border-zinc-300",
            )}
          >
            {t.label} <span className="tabular-nums opacity-80">({count(t.id)})</span>
          </button>
        ))}
      </div>
      {notice && (
        <div className="px-5 pt-4">
          <InlineNotice tone="bad">{notice}</InlineNotice>
        </div>
      )}
      <div id={`${tabsId}-panel`} role="tabpanel" aria-labelledby={`${tabsId}-${tab}`}>
        {shown.length ? (
          <ul className="divide-y divide-zinc-100">
            {shown.map((lesson) => (
              <LessonRow
                key={lesson.id}
                lesson={lesson}
                maxChars={maxChars}
                busy={busy === lesson.id}
                onApprove={() => act(lesson.id, () => client.approve(lesson.id), "Approved: the agent will use it.")}
                onReject={() => act(lesson.id, () => client.reject(lesson.id), "Rejected and archived.")}
                onArchive={() => act(lesson.id, () => client.archive(lesson.id), "Archived.")}
                onRestore={() => act(lesson.id, () => client.restore(lesson.id), "Restored: the agent will use it.")}
                onEdit={(text) => act(lesson.id, () => client.editLesson(lesson.id, text), "Saved.")}
                onRevert={(revision) => act(lesson.id, () => client.revert(lesson.id, revision), "Rolled back.")}
              />
            ))}
          </ul>
        ) : (
          <EmptyState compact title={tab === "pending" ? "Nothing waiting for review" : tab === "active" ? "No lessons in use" : "Nothing archived"}>
            {tab === "pending"
              ? `Lessons the agent proposes (or that people other than admins state) wait here up to ${reviewDays} days.`
              : tab === "active"
                ? "Approved lessons, and lessons an admin states, appear here and reach the agent on every run."
                : `Rejected and archived lessons can be restored for ${reviewDays} days.`}
          </EmptyState>
        )}
      </div>
    </div>
  );
}

function LessonRow({
  lesson,
  maxChars,
  busy,
  onApprove,
  onReject,
  onArchive,
  onRestore,
  onEdit,
  onRevert,
}: {
  lesson: Lesson;
  maxChars: number;
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
  onArchive: () => void;
  onRestore: () => void;
  onEdit: (text: string) => void;
  onRevert: (revision: number) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [draft, setDraft] = useState(lesson.text);
  const fieldId = useId();
  const earlier = revertOptions(lesson);

  const save = (e: FormEvent) => {
    e.preventDefault();
    const text = draft.trim();
    setEditing(false);
    if (text && text !== lesson.text) onEdit(text);
  };

  return (
    <li className="px-5 py-3.5" aria-busy={busy || undefined}>
      {editing ? (
        <form onSubmit={save} className="flex flex-col gap-2 sm:flex-row">
          <label htmlFor={fieldId} className="sr-only">
            Lesson text
          </label>
          <input
            id={fieldId}
            value={draft}
            maxLength={maxChars}
            autoFocus
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setEditing(false)}
            className="h-9 min-w-0 flex-1 rounded-md border border-zinc-300 px-3 text-sm focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/30"
          />
          <div className="flex gap-2">
            <Button type="submit" size="sm" variant="primary">
              <Check aria-hidden /> Save
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>
              <X aria-hidden /> Cancel
            </Button>
          </div>
        </form>
      ) : (
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <p className="break-words text-sm text-zinc-900">{lesson.text}</p>
            <div className="mt-1 flex flex-wrap items-center gap-1.5 text-2xs text-zinc-500">
              <StatusBadge tone={lesson.origin === "untrusted" ? "warn" : lesson.origin === "admin" ? "ok" : "info"}>
                {ORIGIN_LABEL[lesson.origin]}
              </StatusBadge>
              <span>
                updated <time title={dateTime(lesson.updated_at)}>{timeAgo(lesson.updated_at)}</time>
              </span>
              <span>· {lesson.status === "active" ? "kept" : "expires"} {timeUntil(lesson.expires_at)}</span>
              {lesson.run_id && (
                <Tag mono className="max-w-[12rem] truncate">
                  run {lesson.run_id}
                </Tag>
              )}
              {busy && <span>Saving…</span>}
            </div>
          </div>
          <div className="flex shrink-0 flex-wrap gap-1">
            {lesson.status === "pending" && (
              <>
                <Button size="sm" variant="primary" disabled={busy} onClick={onApprove}>
                  <Check aria-hidden /> Approve
                </Button>
                <Button size="sm" variant="danger-outline" disabled={busy} onClick={onReject}>
                  <X aria-hidden /> Reject
                </Button>
              </>
            )}
            {lesson.status !== "archived" && (
              <Button size="icon" variant="ghost" aria-label={`Edit: ${lesson.text}`} disabled={busy} onClick={() => (setDraft(lesson.text), setEditing(true))}>
                <Pencil aria-hidden />
              </Button>
            )}
            {lesson.status === "active" && (
              <Button size="icon" variant="ghost" aria-label={`Archive: ${lesson.text}`} disabled={busy} onClick={onArchive}>
                <Archive aria-hidden />
              </Button>
            )}
            {lesson.status === "archived" && (
              <Button size="sm" variant="secondary" disabled={busy} onClick={onRestore}>
                <RotateCcw aria-hidden /> Restore
              </Button>
            )}
            <Button
              size="icon"
              variant="ghost"
              aria-label={`History of: ${lesson.text}`}
              aria-expanded={showHistory}
              onClick={() => setShowHistory((v) => !v)}
            >
              <History aria-hidden />
            </Button>
          </div>
        </div>
      )}
      {showHistory && (
        <div className="mt-3 rounded-md border border-zinc-100 bg-zinc-50/60 p-3">
          <ol className="space-y-1.5 text-[13px]" aria-label="History, newest first">
            {[...lesson.history].reverse().map((h, i) => (
              <li key={`${h.at}-${i}`} className="flex flex-wrap gap-x-2 text-zinc-600">
                <span className="font-medium capitalize text-zinc-800">{h.action}</span>
                <time title={dateTime(h.at)}>{timeAgo(h.at)}</time>
                <span className="font-mono text-2xs text-zinc-500">{h.by}</span>
                <span className="w-full break-words text-zinc-500">“{h.text}”</span>
              </li>
            ))}
          </ol>
          {lesson.status !== "archived" && earlier.length > 0 && (
            <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-zinc-200 pt-3">
              <span className="text-[13px] text-zinc-600">Roll back to:</span>
              {earlier.map((e) => (
                <Button key={e.revision} size="sm" variant="secondary" disabled={busy} onClick={() => onRevert(e.revision)} title={e.text}>
                  <Undo2 aria-hidden /> <span className="max-w-[14rem] truncate">{e.text}</span>
                </Button>
              ))}
            </div>
          )}
        </div>
      )}
    </li>
  );
}

function AddLessonForm({ maxChars, onAdd }: { maxChars: number; onAdd: (text: string) => Promise<boolean> }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const id = useId();
  const hint = useId();
  const trimmed = text.trim();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!trimmed || busy) return;
    setBusy(true);
    try {
      if (await onAdd(trimmed)) setText("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-2">
      <label htmlFor={id} className="block text-[13px] font-medium text-zinc-800">
        Teach the agent a lesson
      </label>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          id={id}
          value={text}
          maxLength={maxChars}
          placeholder="e.g. Stripe decline code 51 means insufficient funds"
          aria-describedby={hint}
          onChange={(e) => setText(e.target.value)}
          className="h-9 min-w-0 flex-1 rounded-md border border-zinc-300 px-3 text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/30"
        />
        <Button type="submit" variant="primary" disabled={!trimmed || busy}>
          <Plus aria-hidden /> {busy ? "Saving…" : "Save lesson"}
        </Button>
      </div>
      <p id={hint} className="text-2xs text-zinc-500">
        Shared by every run of this agent and used at once, because you are a memory admin. About the work, never about a person; personal data and
        instructions are refused.
      </p>
    </form>
  );
}
