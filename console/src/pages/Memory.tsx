// Memory: what this agent remembers about the signed-in person across conversations, and the controls
// to review, correct, export or erase it. Container only: state lives in useMemory, layout in
// components/memory. Loaded lazily, so agents without memory never download it.

import { Brain, Download, Lock, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";

import { MoreLink } from "@/components/agent";
import { AddMemoryForm } from "@/components/memory/AddMemoryForm";
import { ForgetAllDialog } from "@/components/memory/ForgetAllDialog";
import { MemoryTable } from "@/components/memory/MemoryTable";
import { Button, ButtonLink } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Page, PageHeader } from "@/components/ui/page";
import { EmptyState, ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, Tag } from "@/components/ui/status";
import { useOverview } from "@/lib/data";
import type { MemoryInfo } from "@/lib/memory";
import { useMemory } from "@/lib/useMemory";

const STORE_LABEL: Record<string, string> = { memory: "In-process (lost on restart)", redis: "Redis", cosmos: "Azure Cosmos DB" };

export function MemoryPage() {
  const { data, error, reload } = useOverview();
  return (
    <Page>
      <PageHeader title="Memory" description="What this agent remembers about you between conversations. Only you can see or change it." />
      {error && !data ? (
        <Card>
          <ErrorState title="Unable to load memory" message={error.message} onRetry={reload} />
        </Card>
      ) : !data ? (
        <Loading />
      ) : !data.memory ? (
        <NoMemory />
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
        <MemoryBody info={data.memory} api={data.memory.api} />
      )}
    </Page>
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
      <EmptyState icon={<Brain aria-hidden />} title="This agent doesn't remember people" action={<MoreLink to="/agents/new">See the memory option</MoreLink>}>
        It keeps context within a conversation only. Generate the service with <span className="font-mono">memory=user</span> to let it remember
        preferences across conversations, with screening, retention and per-user controls.
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
