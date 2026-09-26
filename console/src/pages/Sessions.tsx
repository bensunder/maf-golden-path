import { History, RotateCw, Trash2 } from "lucide-react";
import { useState } from "react";

import { MoreLink } from "@/components/agent";
import { Button } from "@/components/ui/button";
import { Card, CardBody, KeyValue } from "@/components/ui/card";
import { CopyButton, Dialog, Sheet } from "@/components/ui/overlay";
import { Page, PageHeader, SectionTitle } from "@/components/ui/page";
import { EmptyState, InlineNotice, Skeleton } from "@/components/ui/states";
import { StatusBadge, type Tone } from "@/components/ui/status";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { ApiError, api } from "@/lib/api";
import { useOverview, useSessions, type KnownSession, type SessionState } from "@/lib/data";
import { dateTime, duration, formatValue, humanize, shortId, timeAgo, timeUntil } from "@/lib/format";

import { AuditTable } from "./Approvals";

function describe(state: SessionState | undefined): { tone: Tone; label: string } {
  if (!state || state.status === "loading") return { tone: "neutral", label: "Loading" };
  if (state.status === "gone") return { tone: "neutral", label: "Expired" };
  if (state.status === "denied") return { tone: "bad", label: "No access" };
  if (state.status === "error") return { tone: "warn", label: "Unavailable" };
  if (state.info.pending.length) return { tone: "pending", label: "Awaiting approval" };
  return { tone: "ok", label: "Active" };
}

export function SessionsPage() {
  const { known, states, refresh } = useSessions();
  const overview = useOverview();
  const [open, setOpen] = useState<KnownSession | null>(null);
  const agent = overview.data?.service.title ?? "—";

  return (
    <Page>
      <PageHeader
        title="Sessions"
        description="Active conversations and durable agent state."
        actions={
          <Button size="sm" onClick={() => void refresh()} aria-label="Refresh sessions">
            <RotateCw aria-hidden /> Refresh
          </Button>
        }
      />
      {overview.data && (
        <InlineNotice className="mb-6">
          Sessions are private to the user who started them, so this list shows the sessions started from this browser.{" "}
          {overview.data.sessions.shared
            ? `They're stored in ${overview.data.sessions.store} and survive restarts and scale-out.`
            : "They're held in memory by this replica and end when it restarts."}{" "}
          Each lasts {duration(overview.data.sessions.ttl_seconds)} after its last message.
        </InlineNotice>
      )}
      <Card>
        {known.length === 0 ? (
          <EmptyState icon={<History aria-hidden />} title="No sessions yet" action={<MoreLink to="/playground">Start a conversation</MoreLink>}>
            A session starts with the first message in the playground. It keeps the conversation, pending approvals and the audit trail.
          </EmptyState>
        ) : (
          <Table label="Sessions">
            <thead>
              <tr>
                <Th>Session ID</Th>
                <Th>Agent</Th>
                <Th>Status</Th>
                <Th className="hidden md:table-cell">Started here</Th>
                <Th className="hidden md:table-cell">Expires</Th>
              </tr>
            </thead>
            <tbody>
              {known.map((s) => {
                const state = states[s.id];
                const d = describe(state);
                return (
                  <Tr key={s.id} interactive onClick={() => setOpen(s)}>
                    <Td>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          setOpen(s);
                        }}
                        className="rounded font-mono text-[13px] text-zinc-900 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
                        aria-label={`Open session ${shortId(s.id)}`}
                      >
                        {shortId(s.id)}…
                      </button>
                    </Td>
                    <Td>{agent}</Td>
                    <Td>{state?.status === "loading" || !state ? <Skeleton className="h-4 w-20" /> : <StatusBadge tone={d.tone}>{d.label}</StatusBadge>}</Td>
                    <Td className="hidden whitespace-nowrap md:table-cell" title={dateTime(s.created)}>
                      {s.source === "playground" ? timeAgo(s.created) : `Opened by ID ${timeAgo(s.created)}`}
                    </Td>
                    <Td className="hidden whitespace-nowrap md:table-cell">{state?.status === "ok" ? timeUntil(state.info.expires_at) : "—"}</Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
      <SessionSheet session={open} onClose={() => setOpen(null)} />
    </Page>
  );
}

function SessionSheet({ session, onClose }: { session: KnownSession | null; onClose: () => void }) {
  const { states, forget, refresh } = useSessions();
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const state = session ? states[session.id] : undefined;
  const d = describe(state);

  const remove = async () => {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteSession(session.id);
      forget(session.id);
      setConfirm(false);
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The session couldn't be deleted.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet open={session !== null} onOpenChange={(o) => !o && onClose()} title={session ? `Session ${shortId(session.id)}` : ""} description="Metadata only. Conversation content isn't shown here.">
      {session && (
        <div className="space-y-6 px-5 py-5">
          <dl className="grid grid-cols-2 gap-x-6 gap-y-4">
            <KeyValue label="Status">
              <StatusBadge tone={d.tone}>{d.label}</StatusBadge>
            </KeyValue>
            <KeyValue label="Channel">
              {state?.status !== "ok" ? "—" : state.info.channel === "teams" ? "Teams" : state.info.channel === "agui" ? "Web (AG-UI)" : "JSON API"}
            </KeyValue>
            <KeyValue label={session.source === "playground" ? "Started here" : "Opened by ID"}>{dateTime(session.created)}</KeyValue>
            <KeyValue label="Expires">{state?.status === "ok" ? `${dateTime(state.info.expires_at)} (${timeUntil(state.info.expires_at)})` : "—"}</KeyValue>
            <div className="col-span-2">
              <KeyValue label="Session ID" mono>
                <span className="flex items-center gap-1">
                  <span className="truncate">{session.id}</span>
                  <CopyButton value={session.id} label="Copy session ID" />
                </span>
              </KeyValue>
            </div>
          </dl>

          {state?.status === "denied" || state?.status === "error" ? (
            <InlineNotice tone="bad">{state.message}</InlineNotice>
          ) : state?.status === "gone" ? (
            <InlineNotice>This session has expired or was deleted. Its audit trail went with it; approvals are also in the service's traces.</InlineNotice>
          ) : state?.status === "ok" ? (
            <>
              <div>
                <SectionTitle>Pending approvals</SectionTitle>
                {state.info.pending.length === 0 ? (
                  <p className="text-[13px] text-zinc-500">None.</p>
                ) : (
                  <ul className="space-y-2">
                    {state.info.pending.map((p) => (
                      <li key={p.id} className="rounded-md border border-amber-200 bg-amber-50/40 px-3 py-2 text-sm">
                        <div className="font-medium text-zinc-900">{humanize(p.tool)}</div>
                        <div className="text-xs text-zinc-600">
                          {Object.entries(p.arguments)
                            .map(([k, v]) => `${humanize(k)} ${formatValue(v)}`)
                            .join(" · ")}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div>
                <SectionTitle>Audit trail</SectionTitle>
                {state.info.audit.length === 0 ? (
                  <p className="text-[13px] text-zinc-500">No approval decisions in this session.</p>
                ) : (
                  <Card>
                    <AuditTable entries={[...state.info.audit].reverse()} />
                  </Card>
                )}
              </div>
            </>
          ) : (
            <CardBody className="space-y-2 px-0">
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-2/3" />
            </CardBody>
          )}

          <div className="flex flex-wrap justify-between gap-2 border-t border-zinc-100 pt-4">
            <Button size="sm" onClick={() => void refresh(session.id)}>
              <RotateCw aria-hidden /> Refresh
            </Button>
            {state?.status === "ok" && state.info.yours ? (
              <Button size="sm" variant="danger-outline" onClick={() => setConfirm(true)}>
                <Trash2 aria-hidden /> Delete session
              </Button>
            ) : (
              <Button size="sm" variant="ghost" onClick={() => (forget(session.id), onClose())}>
                Remove from list
              </Button>
            )}
          </div>
        </div>
      )}
      <Dialog
        open={confirm}
        onOpenChange={setConfirm}
        title="Delete this session?"
        description="The conversation, pending approvals and its audit trail are deleted from the session store. This can't be undone."
        footer={
          <>
            <Button size="sm" onClick={() => setConfirm(false)} disabled={busy}>
              Cancel
            </Button>
            <Button size="sm" variant="danger" onClick={() => void remove()} disabled={busy}>
              {busy ? "Deleting…" : "Delete session"}
            </Button>
          </>
        }
      >
        {error && <InlineNotice tone="bad">{error}</InlineNotice>}
      </Dialog>
    </Sheet>
  );
}
