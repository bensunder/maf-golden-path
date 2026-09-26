import { CircleCheck, Clock3, RotateCw, Search, UserCheck } from "lucide-react";
import { useMemo, useState } from "react";

import { MoreLink } from "@/components/agent";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/overlay";
import { Page, PageHeader, SectionTitle } from "@/components/ui/page";
import { EmptyState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge } from "@/components/ui/status";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { ApiError, api, type AuditEntry, type PendingApproval, type SessionInfo } from "@/lib/api";
import { useOverview, useSessions } from "@/lib/data";
import { dateTime, formatValue, humanize, shortId, timeAgo } from "@/lib/format";

export function ApprovalsPage() {
  const { states, known, refresh, remember } = useSessions();
  const overview = useOverview();
  const mode = overview.data?.approvals.mode;
  // Shown above the list: a decided card leaves the list as soon as the session refreshes.
  const [notice, setNotice] = useState<string | null>(null);
  const loading = known.some((k) => !states[k.id] || states[k.id].status === "loading");

  const sessions = useMemo(
    () => Object.values(states).flatMap((s) => (s.status === "ok" && s.info.pending.length ? [s.info] : [])),
    [states],
  );
  const decided = useMemo(
    () =>
      Object.entries(states)
        .flatMap(([sid, s]) => (s.status === "ok" ? s.info.audit.map((a) => ({ ...a, session: sid })) : []))
        .sort((a, b) => b.decided_at - a.decided_at)
        .slice(0, 20),
    [states],
  );

  return (
    <Page>
      <PageHeader
        title="Approvals"
        description="Review actions requiring human authorization."
        actions={
          <Button size="sm" onClick={() => void refresh()} aria-label="Refresh approvals">
            <RotateCw aria-hidden /> Refresh
          </Button>
        }
      />

      {mode && (
        <InlineNotice className="mb-6" icon={<UserCheck aria-hidden />}>
          {mode === "confirmation" ? (
            <>The person who asked confirms each action. Every decision is recorded in the session's audit trail.</>
          ) : mode === "separation" ? (
            <>
              Only an approver with the <span className="font-mono">{overview.data?.approvals.approver_role}</span> role decides, and never
              on their own request (separation of duties).
            </>
          ) : (
            <>
              Approvers with the <span className="font-mono">{overview.data?.approvals.approver_role}</span> role decide.
            </>
          )}{" "}
          This list covers sessions started from this browser; approvers can open any request by its session ID.
        </InlineNotice>
      )}

      {notice && (
        <div role="status" aria-live="polite" className="mb-4">
          <InlineNotice icon={<CircleCheck aria-hidden />}>
            <span className="whitespace-pre-wrap">{notice}</span>
          </InlineNotice>
        </div>
      )}

      <SectionTitle action={<LookupSession onFound={(id) => remember({ id, created: Date.now() / 1000, source: "lookup" })} />}>Pending</SectionTitle>
      {loading && sessions.length === 0 ? (
        <LoadingRegion label="Loading approvals" className="grid gap-4 lg:grid-cols-2">
          {[0, 1].map((i) => (
            <Card key={i} className="space-y-3 p-5">
              <Skeleton className="h-4 w-40" />
              <Skeleton className="h-3 w-full" />
              <Skeleton className="h-3 w-2/3" />
            </Card>
          ))}
        </LoadingRegion>
      ) : sessions.length === 0 ? (
        <Card>
          <EmptyState icon={<CircleCheck aria-hidden />} title="No pending approvals" action={<MoreLink to="/playground">Open the playground</MoreLink>}>
            When the agent asks for approval in one of your sessions, the request waits here. Try a large refund in the playground.
          </EmptyState>
        </Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {sessions.map((s) => (
            <ApprovalCard key={s.id} session={s} mode={mode ?? "confirmation"} isApprover={Boolean(overview.data?.caller.is_approver)} onDecided={setNotice} />
          ))}
        </div>
      )}

      <SectionTitle>Recent decisions</SectionTitle>
      <Card>
        {decided.length === 0 ? (
          <EmptyState compact title="No decisions yet">
            Decisions in your sessions appear here with who decided and when.
          </EmptyState>
        ) : (
          <AuditTable entries={decided} />
        )}
      </Card>
    </Page>
  );
}

export function AuditTable({ entries }: { entries: (AuditEntry & { session?: string })[] }) {
  return (
    <Table label="Approval decisions">
      <thead>
        <tr>
          <Th>Action</Th>
          <Th>Decision</Th>
          <Th className="hidden md:table-cell">Decided by</Th>
          <Th className="hidden lg:table-cell">Requested by</Th>
          <Th className="hidden sm:table-cell">When</Th>
        </tr>
      </thead>
      <tbody>
        {entries.map((a) => (
          <Tr key={a.id}>
            <Td>
              <div className="text-zinc-900">{humanize(a.tool)}</div>
              <div className="max-w-[260px] truncate text-xs text-zinc-500">
                {Object.entries(a.arguments)
                  .map(([k, v]) => `${humanize(k)} ${formatValue(v)}`)
                  .join(" · ")}
              </div>
            </Td>
            <Td>
              <StatusBadge tone={a.approved ? "ok" : "bad"}>{a.approved ? "Approved" : "Rejected"}</StatusBadge>
              {a.comment && <div className="mt-1 max-w-[220px] truncate text-xs text-zinc-500" title={a.comment}>“{a.comment}”</div>}
            </Td>
            <Td className="hidden md:table-cell">
              <div className="truncate">{a.decided_by_name || a.decided_by || "—"}</div>
              {a.channel && <div className="text-xs text-zinc-500">via {a.channel === "agui" ? "web" : a.channel}</div>}
            </Td>
            <Td className="hidden lg:table-cell">{a.requested_by || "—"}</Td>
            <Td className="hidden whitespace-nowrap sm:table-cell" title={dateTime(a.decided_at)}>
              {timeAgo(a.decided_at)}
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

function ApprovalCard({
  session,
  mode,
  isApprover,
  onDecided,
}: {
  session: SessionInfo;
  mode: "confirmation" | "approver" | "separation";
  isApprover: boolean;
  onDecided: (text: string) => void;
}) {
  const { refresh } = useSessions();
  const [choices, setChoices] = useState<Record<string, boolean>>({});
  const [dialog, setDialog] = useState<null | "approve" | "reject" | "review">(null);
  // What the person reviewed: decisions go out for exactly these items, even if a refresh changes the list.
  const [reviewed, setReviewed] = useState<PendingApproval[]>([]);
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const ownRequest = session.yours;
  const separation = mode === "separation";
  // The same rule as ConversationService.check_decider.
  const canDecide = mode === "confirmation" ? ownRequest : mode === "approver" ? isApprover : isApprover && !ownRequest;
  const openDialog = (kind: "approve" | "reject" | "review") => {
    setReviewed(session.pending);
    setChoices({});
    setDialog(kind);
  };
  const multiple = session.pending.length > 1;
  const title = multiple ? `${session.pending.length} actions` : `${humanize(session.pending[0].tool)} request`;
  const requestedAt = Math.min(...session.pending.map((p) => p.requested_at ?? Infinity));

  const submit = async (decisions: { id: string; approved: boolean }[]) => {
    setSubmitting(true);
    setResult(null);
    try {
      const reply = await api.decide(
        session.id,
        decisions.map((d) => ({ ...d, ...(comment.trim() ? { comment: comment.trim() } : {}) })),
      );
      // The agent's reply belongs to the requester's conversation: show it only to them.
      const what = reviewed.map((p) => humanize(p.tool)).join(", ");
      onDecided(
        ownRequest && reply.reply
          ? `Decision recorded for ${what}. The agent replied: ${reply.reply}`
          : `Decision recorded for ${what}. The agent has resumed the requester's conversation.`,
      );
      setDialog(null);
      setComment("");
      await refresh(session.id);
    } catch (err) {
      setResult({ ok: false, text: err instanceof ApiError ? err.message : "The decision couldn't be submitted." });
    } finally {
      setSubmitting(false);
    }
  };

  const decisionsFor = (approved: boolean) => reviewed.map((p) => ({ id: p.id, approved }));
  const reviewDecisions = reviewed.map((p) => ({ id: p.id, approved: choices[p.id] }));
  const allChosen = reviewed.every((p) => typeof choices[p.id] === "boolean");

  return (
    <Card className="flex flex-col">
      <CardHeader
        title={title}
        description={
          <>
            Session <span className="font-mono">{shortId(session.id)}</span>
            {Number.isFinite(requestedAt) && <> · requested {timeAgo(requestedAt)}</>}
          </>
        }
        action={<StatusBadge tone="pending">Pending</StatusBadge>}
      />
      <div className="flex-1 space-y-4 px-5 py-4">
        {session.pending.map((p) => (
          <PendingDetails key={p.id} item={p} showTool={multiple} />
        ))}
        <dl className="grid grid-cols-[minmax(110px,auto)_1fr] gap-x-6 gap-y-1.5 border-t border-zinc-100 pt-3 text-sm">
          <dt className="text-zinc-500">Requested by</dt>
          <dd className="text-zinc-900">{ownRequest ? "You" : "Another user"}</dd>
        </dl>
        {result && (
          <InlineNotice tone={result.ok ? "info" : "bad"}>
            <span className="whitespace-pre-wrap">{result.text}</span>
          </InlineNotice>
        )}
        {!canDecide && (
          <InlineNotice icon={<Clock3 aria-hidden />}>
            {separation && ownRequest
              ? "Waiting for an approver. You can't approve your own request."
              : mode !== "confirmation" && !isApprover
                ? "Waiting for an approver."
                : "Only the person who asked can confirm this request."}
          </InlineNotice>
        )}
      </div>
      {canDecide && (
        <div className="flex justify-end gap-2 border-t border-zinc-100 bg-zinc-50/50 px-5 py-3">
          {multiple ? (
            <Button size="sm" variant="primary" onClick={() => openDialog("review")}>
              Review and decide
            </Button>
          ) : (
            <>
              <Button size="sm" variant="danger-outline" onClick={() => openDialog("reject")}>
                Reject
              </Button>
              <Button size="sm" variant="primary" onClick={() => openDialog("approve")}>
                Approve
              </Button>
            </>
          )}
        </div>
      )}
      <Dialog
        open={dialog !== null}
        onOpenChange={(o) => !o && !submitting && setDialog(null)}
        title={dialog === "review" ? "Decide each action" : `${dialog === "approve" ? "Approve" : "Reject"} ${humanize(reviewed[0]?.tool ?? "action")}?`}
        description="Your decision resumes the agent and is recorded in the audit trail with your name."
        footer={
          <>
            <Button size="sm" onClick={() => setDialog(null)} disabled={submitting}>
              Cancel
            </Button>
            <Button
              size="sm"
              variant={dialog === "reject" ? "danger" : "primary"}
              disabled={submitting || (dialog === "review" && !allChosen)}
              onClick={() => void submit(dialog === "review" ? (reviewDecisions as { id: string; approved: boolean }[]) : decisionsFor(dialog === "approve"))}
            >
              {submitting ? "Submitting…" : dialog === "review" ? "Submit decisions" : dialog === "approve" ? "Approve" : "Reject"}
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          {reviewed.map((p) => (
            <div key={p.id} className="rounded-md border border-zinc-200 p-3">
              <PendingDetails item={p} showTool />
              {dialog === "review" && (
                <fieldset className="mt-3 flex gap-4 text-sm">
                  <legend className="sr-only">Decision for {humanize(p.tool)}</legend>
                  {[true, false].map((approved) => (
                    <label key={String(approved)} className="flex items-center gap-2">
                      <input
                        type="radio"
                        name={`decision-${p.id}`}
                        checked={choices[p.id] === approved}
                        onChange={() => setChoices((c) => ({ ...c, [p.id]: approved }))}
                        className="accent-zinc-900"
                      />
                      {approved ? "Approve" : "Reject"}
                    </label>
                  ))}
                </fieldset>
              )}
            </div>
          ))}
          <div>
            <label htmlFor={`c-${session.id}`} className="text-[13px] font-medium text-zinc-700">
              Comment <span className="font-normal text-zinc-500">(optional)</span>
            </label>
            <textarea
              id={`c-${session.id}`}
              value={comment}
              maxLength={1000}
              rows={2}
              onChange={(e) => setComment(e.target.value)}
              className="mt-1 w-full resize-none rounded-md border border-zinc-200 px-3 py-2 text-sm outline-none focus:border-zinc-400 focus:ring-2 focus:ring-accent-500/20"
            />
          </div>
          {result && !result.ok && <InlineNotice tone="bad">{result.text}</InlineNotice>}
        </div>
      </Dialog>
    </Card>
  );
}

function PendingDetails({ item, showTool }: { item: PendingApproval; showTool?: boolean }) {
  return (
    <div>
      {showTool && <div className="mb-1.5 text-[13px] font-medium text-zinc-900">{humanize(item.tool)}</div>}
      <dl className="grid grid-cols-[minmax(110px,auto)_1fr] gap-x-6 gap-y-1.5 text-sm">
        {Object.entries(item.arguments).map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-zinc-500">{humanize(k)}</dt>
            <dd className="break-words text-zinc-900">{formatValue(v)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function LookupSession({ onFound }: { onFound: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const find = async () => {
    const id = value.trim();
    if (!id) return;
    setBusy(true);
    setError(null);
    try {
      await api.session(id);
      onFound(id);
      setOpen(false);
      setValue("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The session couldn't be opened.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button size="sm" variant="ghost" onClick={() => setOpen(true)}>
        <Search aria-hidden /> Open by session ID
      </Button>
      <Dialog
        open={open}
        onOpenChange={setOpen}
        title="Open a session"
        description="Approvers can open a request by its session ID (from the requester, a Teams card, or an alert)."
        footer={
          <>
            <Button size="sm" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button size="sm" variant="primary" disabled={!value.trim() || busy} onClick={() => void find()}>
              {busy ? "Opening…" : "Open"}
            </Button>
          </>
        }
      >
        <label htmlFor="lookup-id" className="text-[13px] font-medium text-zinc-700">
          Session ID
        </label>
        <input
          id="lookup-id"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void find()}
          placeholder="agui-…"
          className="mt-1 h-9 w-full rounded-md border border-zinc-200 px-3 font-mono text-sm outline-none focus:border-zinc-400 focus:ring-2 focus:ring-accent-500/20"
        />
        {error && (
          <p role="alert" className="mt-2 text-[13px] text-red-700">
            {error}
          </p>
        )}
      </Dialog>
    </>
  );
}
