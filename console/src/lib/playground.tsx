// Playground state lives above the router, so a conversation survives moving between pages.
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError } from "./api";
import { runAgent, uuid, type AgUiEvent, type Citation, type Interrupt } from "./agui";
import { useOverview, useSessions } from "./data";

export type ChatItem =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string; text: string; streaming: boolean; citations: Citation[]; blocked?: string }
  | { kind: "tool"; id: string; name: string; args: string; status: "running" | "done" | "awaiting" | "rejected" }
  | { kind: "approval"; id: string; interrupts: Interrupt[]; state: "open" | "submitting" | "decided" | "waiting"; approved?: boolean; comment?: string; elsewhere?: boolean }
  | { kind: "error"; id: string; message: string; retry?: string }
  | { kind: "notice"; id: string; text: string };

interface PlaygroundValue {
  threadId: string;
  items: ChatItem[];
  busy: boolean;
  thinking: boolean; // a run started but no text has arrived yet
  started: boolean; // the server has a session for this thread
  resuming: boolean; // an approved or rejected action is running: it can't be stopped
  endpoint: string | null; // the AG-UI endpoint, or null when the service has none
  send: (text: string) => Promise<void>;
  decide: (approvalId: string, approved: boolean, comment?: string) => Promise<void>;
  reset: () => void;
  stop: () => void;
  settle: (approvalId: string, approved: boolean, comment?: string) => void;
}

const PlaygroundContext = createContext<PlaygroundValue | null>(null);

export function PlaygroundProvider({ children }: { children: ReactNode }) {
  const [threadId, setThreadId] = useState(uuid);
  const [items, setItems] = useState<ChatItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [started, setStarted] = useState(false);
  const [resuming, setResuming] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const { remember, refresh } = useSessions();
  const overview = useOverview();
  const endpoint = overview.data ? overview.data.channels.find((c) => c.id === "agui")?.path ?? null : null;

  const update = useCallback((id: string, patch: (item: ChatItem) => ChatItem) => {
    setItems((list) => list.map((item) => (item.id === id ? patch(item) : item)));
  }, []);

  const run = useCallback(
    async (input: { message?: string; resume?: Parameters<typeof runAgent>[1]["resume"] }, retryText?: string): Promise<boolean> => {
      let failed = false;
      let runStarted = false;
      const controller = new AbortController();
      abort.current = controller;
      setBusy(true);
      setThinking(true);
      let currentMessage: string | null = null;
      const toolByCall = new Map<string, string>();

      const onEvent = (event: AgUiEvent) => {
        switch (event.type) {
          case "RUN_STARTED":
            runStarted = true;
            break;
          case "TEXT_MESSAGE_START": {
            const e = event as { messageId: string };
            currentMessage = e.messageId;
            setThinking(false);
            setItems((l) => [...l, { kind: "assistant", id: e.messageId, text: "", streaming: true, citations: [] }]);
            break;
          }
          case "TEXT_MESSAGE_CONTENT": {
            const e = event as { messageId: string; delta: string };
            setThinking(false);
            setItems((l) => {
              if (!l.some((i) => i.id === e.messageId))
                return [...l, { kind: "assistant", id: e.messageId, text: e.delta, streaming: true, citations: [] }];
              return l.map((i) => (i.id === e.messageId && i.kind === "assistant" ? { ...i, text: i.text + e.delta } : i));
            });
            break;
          }
          case "TEXT_MESSAGE_END": {
            const e = event as { messageId: string };
            update(e.messageId, (i) => (i.kind === "assistant" ? { ...i, streaming: false } : i));
            break;
          }
          case "TOOL_CALL_START": {
            const e = event as { toolCallId: string; toolCallName: string };
            toolByCall.set(e.toolCallId, e.toolCallName);
            setItems((l) => [...l, { kind: "tool", id: e.toolCallId, name: e.toolCallName, args: "", status: "running" }]);
            break;
          }
          case "TOOL_CALL_ARGS": {
            const e = event as { toolCallId: string; delta: string };
            update(e.toolCallId, (i) => (i.kind === "tool" ? { ...i, args: i.args + e.delta } : i));
            break;
          }
          case "TOOL_CALL_END": {
            const e = event as { toolCallId: string };
            update(e.toolCallId, (i) => (i.kind === "tool" && i.status === "running" ? { ...i, status: "done" } : i));
            break;
          }
          case "CUSTOM": {
            const e = event as { name: string; value: any };
            if (e.name === "agentkit.citations" && e.value?.messageId) {
              update(e.value.messageId, (i) => (i.kind === "assistant" ? { ...i, citations: e.value.citations ?? [] } : i));
            } else if (e.name === "agentkit.blocked" && currentMessage) {
              const reason = String(e.value?.reason ?? "blocked");
              update(currentMessage, (i) => (i.kind === "assistant" ? { ...i, blocked: reason } : i));
            }
            break;
          }
          case "RUN_ERROR": {
            const e = event as { message?: string };
            failed = true;
            setItems((l) => [...l, { kind: "error", id: uuid(), message: e.message || "The agent failed to respond. Please try again.", retry: retryText }]);
            break;
          }
          case "RUN_FINISHED": {
            const e = event as { outcome?: { type: string; interrupts?: Interrupt[] } };
            if (e.outcome?.type === "interrupt" && e.outcome.interrupts?.length) {
              const interrupts = e.outcome.interrupts;
              const waiting = interrupts.some((i) => i.metadata?.awaiting === "approver");
              // tool calls paused for approval: mark them as awaiting, not completed
              setItems((l) => {
                const paused = new Set(interrupts.map((i) => i.toolCallId).filter(Boolean));
                const marked = l.map((i) => (i.kind === "tool" && paused.has(i.id) ? { ...i, status: "awaiting" as const } : i));
                return [...marked, { kind: "approval", id: uuid(), interrupts, state: waiting ? "waiting" : "open" }];
              });
            }
            break;
          }
          default:
            break;
        }
      };

      try {
        if (!endpoint) throw new ApiError(0, "This service has no AG-UI endpoint, so the playground can't run.");
        await runAgent(endpoint, { threadId, ...input }, onEvent, controller.signal);
      } catch (err) {
        failed = true;
        if (controller.signal.aborted) {
          setItems((l) => [...l, { kind: "notice", id: uuid(), text: "Stopped. This turn wasn't saved; send the message again to retry." }]);
        } else {
          const message = err instanceof ApiError ? err.message : "The service can't be reached. Check your connection and try again.";
          setItems((l) => [...l, { kind: "error", id: uuid(), message, retry: retryText }]);
        }
      } finally {
        setItems((l) => l.map((i) => (i.kind === "assistant" && i.streaming ? { ...i, streaming: false } : i)));
        setBusy(false);
        setThinking(false);
        abort.current = null;
        // The server saves the session when the turn ends, so only now can the console look it up.
        if (runStarted && !started) {
          setStarted(true);
          remember({ id: `agui-${threadId}`, thread: threadId, created: Date.now() / 1000, source: "playground" });
        } else {
          void refresh(`agui-${threadId}`);
        }
      }
      return !failed;
    },
    [threadId, started, remember, refresh, update, endpoint],
  );

  const send = useCallback(
    async (text: string) => {
      const message = text.trim();
      if (!message || busy) return;
      setItems((l) => [...l, { kind: "user", id: uuid(), text: message }]);
      await run({ message }, message);
    },
    [busy, run],
  );

  const decide = useCallback(
    async (approvalId: string, approved: boolean, comment?: string) => {
      const item = items.find((i) => i.id === approvalId);
      if (!item || item.kind !== "approval" || busy) return;
      update(approvalId, (i) => (i.kind === "approval" ? { ...i, state: "submitting" } : i));
      const resume = item.interrupts.map((i) => ({
        interruptId: i.id,
        status: "resolved" as const,
        payload: comment ? { approved, comment } : { approved },
      }));
      // A decided action runs to completion on the server even if this page goes away, so it can't be stopped.
      setResuming(true);
      const ok = await run({ resume }).finally(() => setResuming(false));
      if (!ok) {
        update(approvalId, (i) => (i.kind === "approval" ? { ...i, state: "open" } : i));
        return;
      }
      update(approvalId, (i) => (i.kind === "approval" ? { ...i, state: "decided", approved, comment } : i));
      setItems((l) => l.map((i) => (i.kind === "tool" && i.status === "awaiting" ? { ...i, status: approved ? "done" : "rejected" } : i)));
    },
    [items, busy, run, update],
  );

  const reset = useCallback(() => {
    abort.current?.abort();
    setThreadId(uuid());
    setItems([]);
    setStarted(false);
  }, []);

  const stop = useCallback(() => {
    if (!resuming) abort.current?.abort();
  }, [resuming]);

  /** The approval was decided somewhere else (the Approvals page, Teams, another tab). */
  const settle = useCallback((approvalId: string, approved: boolean, comment?: string) => {
    setItems((l) => {
      const card = l.find((i) => i.id === approvalId);
      if (!card || card.kind !== "approval" || card.state === "decided") return l;
      const calls = new Set(card.interrupts.map((i) => i.toolCallId).filter(Boolean));
      return l.map((i) => {
        if (i.id === approvalId && i.kind === "approval") return { ...i, state: "decided" as const, approved, comment, elsewhere: true };
        if (i.kind === "tool" && calls.has(i.id) && i.status === "awaiting") return { ...i, status: approved ? ("done" as const) : ("rejected" as const) };
        return i;
      });
    });
  }, []);

  const value = useMemo(
    () => ({ threadId, items, busy, thinking, started, resuming, endpoint, send, decide, reset, stop, settle }),
    [threadId, items, busy, thinking, started, resuming, endpoint, send, decide, reset, stop, settle],
  );
  return <PlaygroundContext.Provider value={value}>{children}</PlaygroundContext.Provider>;
}

export function usePlayground(): PlaygroundValue {
  const ctx = useContext(PlaygroundContext);
  if (!ctx) throw new Error("usePlayground outside PlaygroundProvider");
  return ctx;
}
