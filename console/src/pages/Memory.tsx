// Memory: what this agent remembers about the signed-in person across conversations, with the controls to
// review, correct, export or erase it; and, for memory admins, the agent's own memory: the lessons review
// queue, subject histories and the run journal. Container only: state lives in useMemory and the panels,
// layout in components/memory. Loaded lazily, so agents without memory never download it.

import { BookOpen, Brain, Download, FolderSearch, Lock, ScrollText, ShieldCheck, Trash2, User } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useState, type ReactNode } from "react";

import { MoreLink } from "@/components/agent";
import { AddMemoryForm } from "@/components/memory/AddMemoryForm";
import { ForgetAllDialog } from "@/components/memory/ForgetAllDialog";
import { JournalPanel } from "@/components/memory/JournalPanel";
import { LessonsPanel } from "@/components/memory/LessonsPanel";
import { MemoryTable } from "@/components/memory/MemoryTable";
import { SubjectsPanel } from "@/components/memory/SubjectsPanel";
import { Button, ButtonLink } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Page, PageHeader } from "@/components/ui/page";
import { EmptyState, ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, Tag } from "@/components/ui/status";
import { agentMemoryClient, type AgentMemoryInfo, type AgentMemorySummary } from "@/lib/agentMemory";
import { useOverview } from "@/lib/data";
import { cn } from "@/lib/format";
import type { MemoryInfo } from "@/lib/memory";
import { useMemory } from "@/lib/useMemory";

const STORE_LABEL: Record<string, string> = { memory: "In-process (lost on restart)", redis: "Redis", cosmos: "Azure Cosmos DB" };

export function MemoryPage() {
  const { data, error, reload } = useOverview();
  return (
    <Page>
      <PageHeader
        title="Memory"
        description="What this agent remembers about you between conversations (only you can see or change it), and, for memory admins, what the agent has learned."
      />
      {error && !data ? (
        <Card>
          <ErrorState title="Unable to load memory" message={error.message} onRetry={reload} />
        </Card>
      ) : !data ? (
        <Loading />
      ) : !data.memory ? (
        <NoMemory />
      ) : data.memory.profile === "off" ? (
        <Card>
          <EmptyState icon={<Lock aria-hidden />} title="Memory couldn't start">
            {data.memory.off ?? "The memory configuration was refused."} The agent runs with conversation memory only until this is fixed.
          </EmptyState>
        </Card>
      ) : !data.memory.api ? (
        <Card>
          <EmptyState icon={<Lock aria-hidden />} title="Memory API not enabled">
            The agent remembers people, but this service doesn't expose the memory API, so it can't be reviewed here. Add{" "}
            <span className="font-mono">MemoryChannel</span> to the app's channels.
          </EmptyState>
        </Card>
      ) : !data.caller.user && !data.memory.dev_user ? (
        <Card>
          <EmptyState icon={<Lock aria-hidden />} title="Sign in to see your memory">
            Memory belongs to a signed-in person. This request carried no identity.
          </EmptyState>
        </Card>
      ) : (
        <MemoryTabs info={data.memory} api={data.memory.api} />
      )}
    </Page>
  );
}

type Tab = "you" | "lessons" | "subjects" | "journal";

function MemoryTabs({ info, api }: { info: MemoryInfo; api: string }) {
  const agent = info.agent ?? null;
  const client = useMemo(() => agentMemoryClient(api), [api]);
  const [summary, setSummary] = useState<AgentMemorySummary | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("you");
  const [announcement, setAnnouncement] = useState("");
  const tabsId = useId();

  const loadSummary = useCallback(() => {
    if (!agent) return;
    let alive = true;
    client
      .summary()
      .then((s) => alive && setSummary(s))
      .catch((err) => alive && setSummaryError(err instanceof Error ? err.message : "Something went wrong."));
    return () => {
      alive = false;
    };
  }, [agent, client]);

  useEffect(() => loadSummary(), [loadSummary]);

  if (!agent) return <MemoryBody info={info} api={api} />;

  const admin = summary?.is_admin ?? false;
  const tabs: { id: Tab; label: string; icon: ReactNode; badge?: number }[] = [
    { id: "you", label: "About you", icon: <User aria-hidden /> },
    ...(admin
      ? [
          { id: "lessons" as const, label: "Agent lessons", icon: <BookOpen aria-hidden />, badge: summary?.lessons?.pending || undefined },
          { id: "subjects" as const, label: "Subjects", icon: <FolderSearch aria-hidden /> },
          { id: "journal" as const, label: "Journal", icon: <ScrollText aria-hidden /> },
        ]
      : []),
  ];

  return (
    <div className="space-y-6">
      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>
      {tabs.length > 1 && (
        <div role="tablist" aria-label="Memory" className="flex flex-wrap gap-1.5">
          {tabs.map((t) => (
            <button
              key={t.id}
              id={`${tabsId}-${t.id}`}
              role="tab"
              type="button"
              aria-selected={t.id === tab}
              aria-controls={`${tabsId}-panel`}
              onClick={() => setTab(t.id)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-[13px] [&_svg]:size-3.5",
                t.id === tab ? "border-zinc-900 bg-zinc-900 text-white" : "border-zinc-200 bg-white text-zinc-700 hover:border-zinc-300",
              )}
            >
              {t.icon} {t.label}
              {t.badge ? (
                <span className="ml-0.5 rounded-full bg-amber-100 px-1.5 text-2xs font-semibold text-amber-900" aria-label={`${t.badge} waiting for review`}>
                  {t.badge}
                </span>
              ) : null}
            </button>
          ))}
        </div>
      )}
      <div id={`${tabsId}-panel`} role={tabs.length > 1 ? "tabpanel" : undefined} aria-labelledby={tabs.length > 1 ? `${tabsId}-${tab}` : undefined}>
        {tab === "you" ? (
          <div className="space-y-6">
            <MemoryBody info={info} api={api} />
            <AgentMemoryNotice agent={agent} summary={summary} error={summaryError} />
          </div>
        ) : (
          <Card>
            <CardHeader
              title={tab === "lessons" ? "What the agent has learned" : tab === "subjects" ? "Subject memory" : "Run journal"}
              description={
                tab === "lessons"
                  ? "Shared by every run. Only lessons in use reach the agent; proposals wait for your review."
                  : tab === "subjects"
                    ? "What happened to each customer, invoice or case, so any run (even a scheduled one) knows."
                    : "What each run did, without what was said."
              }
              icon={tab === "lessons" ? <BookOpen aria-hidden /> : tab === "subjects" ? <FolderSearch aria-hidden /> : <ScrollText aria-hidden />}
            />
            {tab === "lessons" ? (
              <LessonsPanel
                client={client}
                maxChars={info.max_chars}
                reviewDays={agent.lesson_review_days}
                announce={(text) => {
                  setAnnouncement(text);
                  loadSummary();
                }}
              />
            ) : tab === "subjects" ? (
              <SubjectsPanel client={client} retentionDays={agent.subject_retention_days} announce={setAnnouncement} />
            ) : (
              <JournalPanel client={client} retentionDays={agent.journal_retention_days} />
            )}
          </Card>
        )}
      </div>
    </div>
  );
}

function AgentMemoryNotice({ agent, summary, error }: { agent: AgentMemoryInfo; summary: AgentMemorySummary | null; error: string | null }) {
  return (
    <Card>
      <CardHeader title="The agent's own memory" icon={<ShieldCheck aria-hidden />} />
      <CardBody className="space-y-3 text-[13px] text-zinc-600">
        <p>
          Besides what it remembers about you, this agent keeps <span className="font-medium text-zinc-900">lessons</span> about doing its job
          (reviewed by a memory admin before use), the <span className="font-medium text-zinc-900">history of each customer or case</span> it works
          on (ids only, kept {agent.subject_retention_days} days), and a <span className="font-medium text-zinc-900">journal</span> of what each run
          did, without what was said (kept {agent.journal_retention_days} days).
        </p>
        {error ? (
          <InlineNotice tone="bad">{error}</InlineNotice>
        ) : summary && !summary.is_admin ? (
          <p>
            {summary.admins_configured
              ? "Memory admins review lessons and can see subject histories and the journal here."
              : "No memory admin is set yet, so the lessons the agent proposes wait unreviewed. An operator sets AGENTKIT_MEMORY_ADMINS."}
          </p>
        ) : null}
        <div className="flex flex-wrap gap-1.5">
          {agent.tools.map((t) => (
            <Tag key={t} mono>
              {t}
            </Tag>
          ))}
        </div>
      </CardBody>
    </Card>
  );
}

function Loading() {
  return (
    <LoadingRegion label="Loading memory" className="space-y-4">
      <Card className="space-y-3 p-6">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
      </Card>
    </LoadingRegion>
  );
}

function NoMemory() {
  return (
    <Card>
      <EmptyState icon={<Brain aria-hidden />} title="This agent keeps the conversation only" action={<MoreLink to="/agents/new">See the memory option</MoreLink>}>
        Long-term memory is turned off for it (<span className="font-mono">AGENTKIT_MEMORY=session</span>), or agentkit-memory isn't installed.
        Remove the setting to let it remember people's preferences, learn reviewed lessons and keep each customer's history.
      </EmptyState>
    </Card>
  );
}

function MemoryBody({ info, api }: { info: MemoryInfo; api: string }) {
  const memory = useMemory(api);
  const { state } = memory;
  const [confirming, setConfirming] = useState(false);
  const pending = useMemo(() => new Set(Object.keys(state.pending)), [state.pending]);
  const max = state.maxItems || info.max_items;
  const retention = state.retentionDays || info.retention_days;

  return (
    <div className="space-y-6">
      {/* Every outcome (saved, forgotten, refused) is read out once by screen readers. */}
      <p className="sr-only" role="status" aria-live="polite">
        {state.announcement}
      </p>

      {info.dev_user && (
        <InlineNotice tone="warn">
          Local stand-in user: requests without a sign-in are remembered as <span className="font-mono">AGENTKIT_MEMORY_DEV_USER</span>. This is
          refused outside local and test.
        </InlineNotice>
      )}

      <Card>
        {/* Not CardHeader: two actions beside a long description need to wrap below it on a phone. */}
        <div className="flex flex-col gap-3 border-b border-zinc-100 px-5 py-3.5 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex min-w-0 items-start gap-2.5">
            <Brain aria-hidden className="mt-0.5 size-4 shrink-0 text-zinc-500" />
            <div className="min-w-0">
              <h2 className="text-[13px] font-semibold text-zinc-900">Remembered about you</h2>
              <p className="mt-0.5 text-[13px] text-zinc-500">
                {state.items.length} of {max} notes · each is forgotten {retention} days after it was last updated
              </p>
            </div>
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            <ButtonLink size="sm" href={memory.exportUrl}>
              <Download aria-hidden /> Export
            </ButtonLink>
            <Button size="sm" variant="danger-outline" disabled={!state.items.length} onClick={() => setConfirming(true)}>
              <Trash2 aria-hidden /> Forget everything
            </Button>
          </div>
        </div>
        <CardBody className="border-b border-zinc-100">
          <AddMemoryForm maxChars={info.max_chars} full={state.items.length >= max} onAdd={memory.add} />
        </CardBody>
        {state.error && state.status !== "error" && (
          <div className="px-5 pt-4">
            <InlineNotice tone="bad">{state.error}</InlineNotice>
          </div>
        )}
        {state.status === "loading" ? (
          <LoadingRegion label="Loading notes" className="space-y-3 p-5">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
          </LoadingRegion>
        ) : state.status === "error" ? (
          <ErrorState compact title="Unable to load your notes" message={state.error ?? "Something went wrong."} onRetry={memory.reload} />
        ) : state.items.length ? (
          <MemoryTable items={state.items} maxChars={info.max_chars} pending={pending} onEdit={memory.edit} onRemove={memory.remove} />
        ) : (
          <EmptyState compact icon={<Brain aria-hidden />} title="Nothing remembered yet">
            When you tell the agent a lasting preference ("always reply in Spanish"), it may remember it here. You can also add one above.
          </EmptyState>
        )}
      </Card>

      <Card>
        <CardHeader title="How memory is governed" icon={<Lock aria-hidden />} />
        <CardBody>
          <ul className="grid gap-x-6 gap-y-3 text-[13px] text-zinc-600 sm:grid-cols-2">
            <li>
              <span className="font-medium text-zinc-900">Yours only.</span> Notes are stored under a keyed hash of your identity; no other user or
              agent can read them.
            </li>
            <li>
              <span className="font-medium text-zinc-900">Screened.</span> Personal data and instructions aimed at the agent are refused before
              they are stored.
            </li>
            <li>
              <span className="font-medium text-zinc-900">Bounded.</span> At most {max} notes of {info.max_chars} characters, kept {retention} days.
            </li>
            <li className="flex flex-wrap items-center gap-1.5">
              <span className="font-medium text-zinc-900">Stored in</span> {STORE_LABEL[info.store] ?? info.store}
              {info.salt_configured ? (
                <StatusBadge tone="ok">Keyed</StatusBadge>
              ) : (
                <StatusBadge tone="warn">Unkeyed (set AGENTKIT_MEMORY_SALT)</StatusBadge>
              )}
            </li>
            <li className="sm:col-span-2">
              <span className="font-medium text-zinc-900">Agent tools:</span>{" "}
              {info.tools.map((t) => (
                <Tag key={t} mono className="mr-1">
                  {t}
                </Tag>
              ))}
            </li>
          </ul>
        </CardBody>
      </Card>

      <ForgetAllDialog open={confirming} onOpenChange={setConfirming} count={state.items.length} onConfirm={memory.forgetAll} />
    </div>
  );
}
