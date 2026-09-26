import { ArrowUp, ChevronRight, CircleCheck, Clock3, FileText, Loader2, Plus, ShieldAlert, Square, TriangleAlert, Wrench, XCircle } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, Eyebrow } from "@/components/ui/card";
import { CopyButton, Dialog } from "@/components/ui/overlay";
import { Page } from "@/components/ui/page";
import { ErrorState, InlineNotice, Skeleton } from "@/components/ui/states";
import { StatusBadge, Tag } from "@/components/ui/status";
import { api, type Overview } from "@/lib/api";
import { safeUrl, type Interrupt } from "@/lib/agui";
import { useLoad, useOverview, useSessions } from "@/lib/data";
import { formatValue, humanize, shortId } from "@/lib/format";
import { usePlayground, type ChatItem } from "@/lib/playground";
import { Link } from "@/lib/router";

export function PlaygroundPage() {
  const overview = useOverview();
  return (
    <Page wide className="flex h-[calc(100dvh-3.5rem)] flex-col pb-6">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-[-0.01em] text-zinc-950">Agent Playground</h1>
          <p className="mt-1 text-sm text-zinc-500">Test your agent in a controlled environment. Runs go through this service's guardrails and approvals, as in every channel.</p>
        </div>
      </header>
      {overview.error && !overview.data ? (
        <Card>
          <ErrorState title="Unable to load the agent" message={overview.error.message} onRetry={overview.reload} />
        </Card>
      ) : (
        <div className="flex min-h-0 flex-1 gap-6">
          <Conversation data={overview.data} />
          <aside aria-label="Run context" className="hidden w-80 shrink-0 overflow-y-auto xl:block">
            <ContextPanel data={overview.data} />
          </aside>
        </div>
      )}
    </Page>
  );
}

// ------------------------------------------------------------------ conversation
function Conversation({ data }: { data: Overview | null }) {
  const pg = usePlayground();
  const [text, setText] = useState("");
  const log = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const stick = useRef(true);
  const evals = useLoad(api.evals);

  const suggestions = useMemo(
    () =>
      (evals.data?.cases ?? [])
        .filter((c) => !c.checks.includes("Refused") && !c.as_user && c.input.length < 140)
        .slice(0, 4)
        .map((c) => c.input),
    [evals.data],
  );

  // Follow the stream unless the reader scrolled up.
  useLayoutEffect(() => {
    const el = log.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [pg.items, pg.thinking]);

  useEffect(() => {
    if (!pg.busy) input.current?.focus({ preventScroll: true });
  }, [pg.busy]);

  const submit = () => {
    if (!text.trim() || pg.busy) return;
    const value = text;
    setText("");
    stick.current = true;
    void pg.send(value);
  };

  const pendingOpen = pg.items.some((i) => i.kind === "approval" && (i.state === "open" || i.state === "waiting"));
  const title = data?.service.title ?? "the agent";

  return (
    <Card className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <div className="flex items-center justify-between gap-3 border-b border-zinc-100 px-5 py-3">
        <div className="min-w-0">
          <div className="truncate text-[13px] font-semibold text-zinc-900">Ask the {title}</div>
          <div className="flex items-center gap-1.5 text-xs text-zinc-500">
            {pg.started ? (
              <>
                <span>Session</span>
                <span className="font-mono">{shortId(pg.threadId)}</span>
                <CopyButton value={`agui-${pg.threadId}`} label="Copy session ID" className="h-6 w-6 [&_svg]:size-3.5" />
              </>
            ) : (
              <span>New session, created on your first message</span>
            )}
          </div>
        </div>
        <Button size="sm" onClick={pg.reset} disabled={pg.items.length === 0 && !pg.busy}>
          <Plus aria-hidden /> New conversation
        </Button>
      </div>

      <div
        ref={log}
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label="Conversation"
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
        }}
        className="flex-1 overflow-y-auto px-5 py-6"
      >
        {data && !pg.endpoint ? (
          <div className="mx-auto flex h-full max-w-lg flex-col items-center justify-center text-center">
            <h2 className="text-sm font-semibold text-zinc-900">The playground needs the AG-UI endpoint</h2>
            <p className="mt-1 text-[13px] leading-relaxed text-zinc-500">
              This service doesn't install <span className="font-mono">AgUiChannel()</span>. Add it (with web chat) to talk to the agent here.
            </p>
          </div>
        ) : pg.items.length === 0 ? (
          <div className="mx-auto flex h-full max-w-lg flex-col items-center justify-center text-center">
            <div className="flex size-10 items-center justify-center rounded-lg border border-zinc-200 bg-zinc-50 text-zinc-500">
              <Wrench aria-hidden className="size-5" />
            </div>
            <h2 className="mt-3 text-sm font-semibold text-zinc-900">Ask the {title}</h2>
            <p className="mt-1 text-[13px] leading-relaxed text-zinc-500">
              {data?.agent.description ?? "Send a message to start a session."} Tool calls, citations and approvals show up as they happen.
            </p>
            {suggestions.length > 0 && (
              <div className="mt-5 w-full">
                <Eyebrow className="mb-2">From the eval cases</Eyebrow>
                <ul className="grid gap-2 sm:grid-cols-2">
                  {suggestions.map((s) => (
                    <li key={s}>
                      <button
                        type="button"
                        onClick={() => {
                          setText(s);
                          input.current?.focus();
                        }}
                        className="w-full rounded-md border border-zinc-200 bg-white px-3 py-2 text-left text-[13px] text-zinc-700 transition-colors hover:border-zinc-300 hover:bg-zinc-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
                      >
                        {s}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        ) : (
          <ol className="mx-auto max-w-3xl space-y-4">
            {pg.items.map((item) => (
              <li key={item.id}>
                <ChatEntry item={item} />
              </li>
            ))}
            {pg.thinking && (
              <li aria-label="The agent is working">
                <div className="flex gap-3">
                  <AgentAvatar />
                  <div className="w-full max-w-md space-y-2 pt-1.5">
                    <Skeleton className="h-3 w-3/4" />
                    <Skeleton className="h-3 w-1/2" />
                  </div>
                </div>
              </li>
            )}
          </ol>
        )}
      </div>

      <form
        className="border-t border-zinc-100 bg-white p-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        {pendingOpen && (
          <InlineNotice tone="warn" icon={<Clock3 aria-hidden />} className="mx-auto mb-2 max-w-3xl">
            This session is paused until the approval above is decided.
          </InlineNotice>
        )}
        <div className="mx-auto flex max-w-3xl items-end gap-2 rounded-lg border border-zinc-200 bg-white p-1.5 transition-colors focus-within:border-zinc-400 focus-within:ring-2 focus-within:ring-accent-500/20">
          <label htmlFor="playground-input" className="sr-only">
            Message
          </label>
          <textarea
            id="playground-input"
            ref={input}
            value={text}
            rows={1}
            maxLength={data?.agent.limits.max_input_chars ?? 20000}
            onChange={(e) => {
              setText(e.target.value);
              e.target.style.height = "auto";
              e.target.style.height = `${Math.min(e.target.scrollHeight, 180)}px`;
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit();
              }
            }}
            disabled={pg.busy}
            placeholder="Ask something…"
            className="max-h-[180px] min-h-[36px] flex-1 resize-none bg-transparent px-2.5 py-2 text-sm text-zinc-900 outline-none placeholder:text-zinc-500 disabled:text-zinc-500"
          />
          {pg.resuming ? (
            <Button size="sm" disabled title="An approved or rejected action always runs to completion">
              <Loader2 aria-hidden className="animate-spin motion-reduce:animate-none" /> Finishing
            </Button>
          ) : pg.busy ? (
            <Button size="sm" onClick={pg.stop} aria-label="Stop">
              <Square aria-hidden className="fill-current" /> Stop
            </Button>
          ) : (
            <Button type="submit" size="sm" variant="primary" disabled={!text.trim()}>
              Send <ArrowUp aria-hidden />
            </Button>
          )}
        </div>
        <p className="mx-auto mt-1.5 max-w-3xl px-1 text-2xs text-zinc-500">
          Enter to send, Shift+Enter for a new line. Runs as you, through the same guardrails as every channel.
        </p>
      </form>
    </Card>
  );
}

function AgentAvatar() {
  return (
    <span aria-hidden className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md border border-zinc-200 bg-white">
      <svg viewBox="0 0 32 32" className="size-4">
        <path d="M9 22.5 16 9l7 13.5" fill="none" stroke="#18181b" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M12.2 17.5h7.6" stroke="#3b6ef6" strokeWidth="3" strokeLinecap="round" />
      </svg>
    </span>
  );
}

function ChatEntry({ item }: { item: ChatItem }) {
  switch (item.kind) {
    case "user":
      return (
        <div className="flex justify-end">
          <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-lg rounded-br-sm bg-zinc-900 px-3.5 py-2 text-sm text-white">
            <span className="sr-only">You: </span>
            {item.text}
          </div>
        </div>
      );
    case "assistant":
      return <AssistantMessage item={item} />;
    case "tool":
      return <ToolTrace item={item} />;
    case "approval":
      return <ApprovalRequest item={item} />;
    case "error":
      return <ChatError item={item} />;
    case "notice":
      return <p className="text-center text-xs text-zinc-500">{item.text}</p>;
  }
}

function AssistantMessage({ item }: { item: Extract<ChatItem, { kind: "assistant" }> }) {
  return (
    <div className="flex gap-3">
      <AgentAvatar />
      <div className="min-w-0 flex-1 pt-0.5">
        <span className="sr-only">Agent: </span>
        {item.blocked && (
          <StatusBadge tone="warn" className="mb-1.5">
            <ShieldAlert aria-hidden className="size-3.5" /> Refused by guardrail · {humanize(item.blocked.split(":")[0])}
          </StatusBadge>
        )}
        <div className="whitespace-pre-wrap break-words text-sm leading-relaxed text-zinc-900">
          {item.text}
          {item.streaming && <span aria-hidden className="ml-0.5 inline-block h-4 w-[2px] translate-y-0.5 animate-pulse bg-zinc-400" />}
        </div>
        {item.citations.length > 0 && (
          <div className="mt-2.5">
            <Eyebrow className="mb-1.5">Sources</Eyebrow>
            <ol aria-label="Sources" className="flex flex-wrap gap-1.5">
              {item.citations.map((c) => {
                const href = safeUrl(c.url);
                const body = (
                  <>
                    <span className="font-mono text-2xs text-zinc-500">[{c.n}]</span>
                    <FileText aria-hidden className="size-3.5 text-zinc-500" />
                    <span className="max-w-[260px] truncate">{c.title || c.id}</span>
                  </>
                );
                return (
                  <li key={`${c.n}-${c.id}`}>
                    {href ? (
                      <a
                        href={href}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs text-zinc-700 hover:border-zinc-300 hover:bg-zinc-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
                      >
                        {body}
                      </a>
                    ) : (
                      <span className="inline-flex items-center gap-1.5 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs text-zinc-700">{body}</span>
                    )}
                  </li>
                );
              })}
            </ol>
          </div>
        )}
      </div>
    </div>
  );
}

function parseArgs(raw: string): Record<string, unknown> | null {
  if (!raw) return {};
  try {
    const value = JSON.parse(raw);
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

function ToolTrace({ item }: { item: Extract<ChatItem, { kind: "tool" }> }) {
  const args = parseArgs(item.args);
  const status =
    item.status === "running" ? (
      <span className="inline-flex items-center gap-1 text-xs text-zinc-500">
        <Loader2 aria-hidden className="size-3.5 animate-spin motion-reduce:animate-none" /> Running
      </span>
    ) : item.status === "awaiting" ? (
      <StatusBadge tone="pending">Awaiting approval</StatusBadge>
    ) : item.status === "rejected" ? (
      <StatusBadge tone="neutral">Not run (rejected)</StatusBadge>
    ) : (
      <span className="inline-flex items-center gap-1 text-xs text-emerald-700">
        <CircleCheck aria-hidden className="size-3.5" /> Completed
      </span>
    );
  return (
    <details className="group ml-10 rounded-md border border-zinc-200 bg-zinc-50/70 open:bg-white">
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-md px-3 py-2 text-[13px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 [&::-webkit-details-marker]:hidden">
        <ChevronRight aria-hidden className="size-3.5 text-zinc-500 transition-transform group-open:rotate-90 motion-reduce:transition-none" />
        <Wrench aria-hidden className="size-3.5 text-zinc-500" />
        <span className="text-zinc-500">Tool</span>
        <span className="truncate font-mono text-[12.5px] text-zinc-900">{item.name}</span>
        <span className="ml-auto shrink-0">{status}</span>
      </summary>
      <div className="border-t border-zinc-100 px-3 py-2.5">
        {args === null ? (
          <pre className="whitespace-pre-wrap break-all font-mono text-xs text-zinc-700">{item.args}</pre>
        ) : Object.keys(args).length === 0 ? (
          <p className="text-xs text-zinc-500">No arguments.</p>
        ) : (
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
            {Object.entries(args).map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-zinc-500">{humanize(k)}</dt>
                <dd className="break-all font-mono text-zinc-900">{formatValue(v)}</dd>
              </div>
            ))}
          </dl>
        )}
        <p className="mt-2 text-2xs text-zinc-500">Tool results stay on the server; the agent summarizes them.</p>
      </div>
    </details>
  );
}

function ApprovalRequest({ item }: { item: Extract<ChatItem, { kind: "approval" }> }) {
  const pg = usePlayground();
  const overview = useOverview();
  const { states, refresh } = useSessions();
  const sessionId = `agui-${pg.threadId}`;
  const server = states[sessionId];
  const ids = item.interrupts.map((i) => i.id);
  const decision = server?.status === "ok" ? server.info.audit.find((a) => ids.includes(a.id)) : undefined;
  const open = item.state === "open" || item.state === "waiting";

  // Decided somewhere else (the Approvals page, Teams, another tab): reflect it here.
  useEffect(() => {
    if (open && decision && server?.status === "ok" && !server.info.pending.some((p) => ids.includes(p.id))) {
      pg.settle(item.id, decision.approved, decision.comment ?? undefined);
    }
  }, [open, decision, server]); // eslint-disable-line react-hooks/exhaustive-deps

  // While it waits, check every few seconds whether someone decided.
  useEffect(() => {
    if (!open || !pg.started) return;
    const timer = window.setInterval(() => void refresh(sessionId), 5000);
    return () => window.clearInterval(timer);
  }, [open, pg.started, sessionId, refresh]);

  const [confirm, setConfirm] = useState<null | boolean>(null);
  const [comment, setComment] = useState("");
  const tools = item.interrupts.map((i) => humanize(i.metadata?.tool ?? "action"));
  const heading = tools.length === 1 ? `${tools[0]} request` : `${tools.length} actions need approval`;
  const role = item.interrupts[0]?.metadata?.approver_role;

  const badge =
    item.state === "decided" ? (
      <StatusBadge tone={item.approved ? "ok" : "bad"}>{item.approved ? "Approved" : "Rejected"}</StatusBadge>
    ) : item.state === "submitting" ? (
      <StatusBadge tone="info">Submitting</StatusBadge>
    ) : (
      <StatusBadge tone="pending">Pending</StatusBadge>
    );

  return (
    <div className="ml-10 overflow-hidden rounded-lg border border-amber-200 bg-white" role="group" aria-label="Approval request">
      <div className="flex items-center justify-between gap-3 border-b border-amber-100 bg-amber-50/60 px-4 py-2.5">
        <div className="flex items-center gap-2 text-[13px] font-semibold text-zinc-900">
          <Clock3 aria-hidden className="size-4 text-amber-600" /> {heading}
        </div>
        {badge}
      </div>
      <div className="space-y-3 px-4 py-3">
        {item.interrupts.map((i) => (
          <ArgumentList key={i.id} interrupt={i} showTool={item.interrupts.length > 1} />
        ))}
        {item.state === "waiting" && (
          <InlineNotice icon={<Clock3 aria-hidden />}>
            Waiting for an approver{role ? <> with the <span className="font-mono">{role}</span> role</> : null}.
            {overview.data?.approvals.mode === "separation" ? " You can't approve your own request." : ""} It appears in their{" "}
            <Link to="/approvals" className="font-medium underline">Approvals</Link> queue.
          </InlineNotice>
        )}
        {item.state === "decided" && item.elsewhere && (
          <InlineNotice icon={item.approved ? <CircleCheck aria-hidden /> : <XCircle aria-hidden />}>
            {item.approved ? "Approved" : "Rejected"}
            {decision ? ` by ${decision.decided_by_name || decision.decided_by || "an approver"}` : ""}. The agent's reply went to whoever
            decided; send a message to continue.
          </InlineNotice>
        )}
        {item.state === "decided" && item.comment && <p className="text-xs text-zinc-500">Comment: {item.comment}</p>}
      </div>
      {(item.state === "open" || item.state === "submitting") && (
        <div className="flex justify-end gap-2 border-t border-zinc-100 bg-zinc-50/50 px-4 py-2.5">
          <Button size="sm" variant="danger-outline" disabled={item.state !== "open" || pg.busy} onClick={() => setConfirm(false)}>
            Reject
          </Button>
          <Button size="sm" variant="primary" disabled={item.state !== "open" || pg.busy} onClick={() => setConfirm(true)}>
            Approve
          </Button>
        </div>
      )}
      <Dialog
        open={confirm !== null}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={confirm ? `Approve ${tools.join(", ")}?` : `Reject ${tools.join(", ")}?`}
        description={confirm ? "The agent will run this action with your authorization. This is recorded in the audit trail." : "The agent will not run this action. This is recorded in the audit trail."}
        footer={
          <>
            <Button size="sm" onClick={() => setConfirm(null)}>
              Cancel
            </Button>
            <Button
              size="sm"
              variant={confirm ? "primary" : "danger"}
              onClick={() => {
                const approved = Boolean(confirm);
                setConfirm(null);
                void pg.decide(item.id, approved, comment.trim() || undefined);
              }}
            >
              {confirm ? "Approve" : "Reject"}
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          {item.interrupts.map((i) => (
            <ArgumentList key={i.id} interrupt={i} showTool />
          ))}
          <div>
            <label htmlFor={`comment-${item.id}`} className="text-[13px] font-medium text-zinc-700">
              Comment <span className="font-normal text-zinc-500">(optional)</span>
            </label>
            <textarea
              id={`comment-${item.id}`}
              value={comment}
              maxLength={1000}
              rows={2}
              onChange={(e) => setComment(e.target.value)}
              className="mt-1 w-full resize-none rounded-md border border-zinc-200 px-3 py-2 text-sm outline-none focus:border-zinc-400 focus:ring-2 focus:ring-accent-500/20"
            />
          </div>
        </div>
      </Dialog>
    </div>
  );
}

function ArgumentList({ interrupt, showTool }: { interrupt: Interrupt; showTool?: boolean }) {
  const args = interrupt.metadata?.arguments ?? {};
  return (
    <div>
      {showTool && <div className="mb-1 font-mono text-xs text-zinc-500">{interrupt.metadata?.tool}</div>}
      <dl className="grid grid-cols-[minmax(110px,auto)_1fr] gap-x-6 gap-y-1.5 text-sm">
        {Object.entries(args).map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-zinc-500">{humanize(k)}</dt>
            <dd className="break-words text-zinc-900">{formatValue(v)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function ChatError({ item }: { item: Extract<ChatItem, { kind: "error" }> }) {
  const pg = usePlayground();
  return (
    <div role="alert" className="ml-10 flex items-start gap-2.5 rounded-md border border-red-200 bg-red-50 px-3.5 py-2.5 text-[13px] text-red-800">
      <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
      <div className="min-w-0 flex-1">{item.message}</div>
      {item.retry && (
        <Button size="sm" variant="secondary" className="-my-1 h-7" disabled={pg.busy} onClick={() => void pg.send(item.retry!)}>
          Retry
        </Button>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ context panel
function ContextPanel({ data }: { data: Overview | null }) {
  const pg = usePlayground();
  if (!data)
    return (
      <div className="space-y-4" aria-hidden>
        <Skeleton className="h-40 w-full rounded-lg" />
        <Skeleton className="h-48 w-full rounded-lg" />
      </div>
    );
  const active = data.security.filter((c) => c.status !== "off");
  return (
    <div className="space-y-4">
      <Card className="px-4 py-3.5">
        <Eyebrow>Session</Eyebrow>
        <dl className="mt-2.5 space-y-2 text-[13px]">
          <Row label="ID">{pg.started ? <span className="font-mono">{shortId(pg.threadId)}</span> : <span className="text-zinc-500">Not started</span>}</Row>
          <Row label="Runs as">{data.caller.user ?? <span className="text-zinc-500">anonymous</span>}</Row>
          <Row label="Channel">AG-UI</Row>
          <Row label="Store">{data.sessions.shared ? data.sessions.store : "memory"}</Row>
        </dl>
        {pg.started && (
          <Link to="/sessions" className="mt-3 inline-block text-xs font-medium text-accent-700 hover:text-accent-800">
            Inspect in Sessions
          </Link>
        )}
      </Card>

      <Card className="px-4 py-3.5">
        <Eyebrow>Agent</Eyebrow>
        <div className="mt-2 text-sm font-medium text-zinc-900">{data.service.title}</div>
        <div className="mt-0.5 font-mono text-xs text-zinc-500">{data.agent.model}</div>
        <ul className="mt-3 space-y-1.5">
          {data.agent.tools.map((t) => (
            <li key={t.name} className="flex items-center justify-between gap-2">
              <span className="truncate font-mono text-xs text-zinc-700">{t.name}</span>
              {t.approval !== "never" && <Tag className="shrink-0 text-2xs">approval</Tag>}
            </li>
          ))}
        </ul>
      </Card>

      <Card className="px-4 py-3.5">
        <Eyebrow>Guardrails in this run</Eyebrow>
        <ul className="mt-2.5 space-y-1.5">
          {active.map((c) => (
            <li key={c.id} className="flex items-center gap-2 text-[13px] text-zinc-700" title={c.detail}>
              {c.status === "on" ? (
                <CircleCheck aria-hidden className="size-3.5 text-emerald-600" />
              ) : (
                <TriangleAlert aria-hidden className="size-3.5 text-amber-600" />
              )}
              <span>{c.name}</span>
              {c.status === "partial" && <span className="sr-only">(partial)</span>}
            </li>
          ))}
        </ul>
        <Link to="/security" className="mt-3 inline-block text-xs font-medium text-accent-700 hover:text-accent-800">
          Security posture
        </Link>
      </Card>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <dt className="text-zinc-500">{label}</dt>
      <dd className="min-w-0 truncate text-right text-zinc-900">{children}</dd>
    </div>
  );
}
