// A small AG-UI client for the playground: POST a RunAgentInput, read server-sent events.
// Everything the agent or a tool produced is treated as text by the UI, never as HTML.

import { errorFrom } from "./api";

export interface Citation {
  n: number;
  id: string;
  title: string;
  url?: string | null;
}

export interface Interrupt {
  id: string;
  reason?: string;
  message?: string;
  toolCallId?: string | null;
  metadata?: {
    tool?: string;
    arguments?: Record<string, unknown>;
    awaiting?: "requester" | "approver";
    approver_role?: string | null;
    status_url?: string;
  };
}

export type AgUiEvent =
  | { type: "RUN_STARTED"; threadId: string; runId: string }
  | { type: "TEXT_MESSAGE_START"; messageId: string }
  | { type: "TEXT_MESSAGE_CONTENT"; messageId: string; delta: string }
  | { type: "TEXT_MESSAGE_END"; messageId: string }
  | { type: "TOOL_CALL_START"; toolCallId: string; toolCallName: string }
  | { type: "TOOL_CALL_ARGS"; toolCallId: string; delta: string }
  | { type: "TOOL_CALL_END"; toolCallId: string }
  | { type: "RUN_ERROR"; message?: string; code?: string }
  | { type: "CUSTOM"; name: string; value: any }
  | { type: "RUN_FINISHED"; outcome?: { type: "success" } | { type: "interrupt"; interrupts: Interrupt[] } }
  | { type: string; [key: string]: unknown };

export function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

/** Split an SSE byte stream into parsed JSON events. Exported for tests. */
export class SseParser {
  private buffer = "";

  push(chunk: string): AgUiEvent[] {
    this.buffer += chunk.replace(/\r\n/g, "\n");
    const events: AgUiEvent[] = [];
    let cut: number;
    while ((cut = this.buffer.indexOf("\n\n")) >= 0) {
      const frame = this.buffer.slice(0, cut);
      this.buffer = this.buffer.slice(cut + 2);
      const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).replace(/^ /, ""))
        .join("\n");
      if (!data) continue;
      try {
        const event = JSON.parse(data);
        if (event && typeof event.type === "string") events.push(event);
      } catch {
        /* a partial or foreign frame: skip it */
      }
    }
    return events;
  }
}

export interface RunInput {
  threadId: string;
  message?: string;
  resume?: { interruptId: string; status: "resolved" | "cancelled"; payload?: { approved: boolean; comment?: string } }[];
}

export async function runAgent(
  endpoint: string,
  input: RunInput,
  onEvent: (event: AgUiEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const body = {
    threadId: input.threadId,
    runId: uuid(),
    state: {},
    tools: [],
    context: [],
    forwardedProps: {},
    messages: input.message ? [{ id: uuid(), role: "user", content: input.message }] : [],
    ...(input.resume ? { resume: input.resume } : {}),
  };
  const response = await fetch(endpoint, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) throw await errorFrom(response);
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    for (const event of parser.push(decoder.decode(value, { stream: true }))) onEvent(event);
  }
}

/** Only http(s) links become links; anything else is shown as text. */
export function safeUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url, window.location.href);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}
