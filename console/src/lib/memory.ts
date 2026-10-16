// Long-term memory: the typed client for the service's /v1/memory API and the page's state as a pure
// reducer (optimistic edits and deletes with rollback). No React here, so it is unit-tested directly.
//
// Every call acts on the signed-in person's own memory: the API has no way to read anyone else's.

import type { AgentMemoryInfo } from "./agentMemory";
import { ApiError, ROOT } from "./api";

export type MemoryKind = "preference" | "fact";
export type MemorySource = "agent" | "user";

export interface MemoryItem {
  id: string;
  text: string;
  kind: MemoryKind;
  source: MemorySource;
  created_at: number;
  updated_at: number;
  expires_at: number;
}

export interface MemoryList {
  items: MemoryItem[];
  count: number;
  max_items: number;
  retention_days: number;
}

/** The overview's description of the agent's memory (null when the agent has none).
 *  ``full`` also has the agent's own memory (lessons, subjects, journal) under ``agent``; ``off`` means memory was
 *  configured but couldn't start (``off`` says why) and nothing else is set. */
export interface MemoryInfo {
  profile: "user" | "full" | "off";
  off?: string | null;
  user?: boolean;
  agent?: AgentMemoryInfo | null;
  store: "memory" | "redis" | "cosmos" | string;
  retention_days: number;
  max_items: number;
  max_chars: number;
  salt_configured: boolean;
  dev_user: boolean;
  tools: string[];
  api: string | null;
}

/** An API failure with the service's stable code, its message, and Retry-After when given. */
export class MemoryApiError extends ApiError {
  constructor(
    status: number,
    message: string,
    readonly code: string,
    readonly retryAfter: number | null = null,
  ) {
    super(status, message);
  }
}

const FALLBACK: Record<number, string> = {
  0: "The service can't be reached. Check your connection and try again.",
  401: "You're not signed in, or your sign-in has expired. Reload the page to sign in again.",
  404: "That memory no longer exists.",
  409: "The memory changed in another window. Reload and try again.",
  415: "The request was not sent as JSON.",
  429: "Too many changes in a short time.",
  503: "Memory is temporarily unavailable.",
};

export async function memoryError(response: Response): Promise<MemoryApiError> {
  let code = "http_" + response.status;
  let message: string | null = null;
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      if (typeof detail.code === "string") code = detail.code;
      if (typeof detail.message === "string") message = detail.message;
    } else if (typeof detail === "string") {
      message = detail;
    } else if (Array.isArray(detail) && detail[0]?.msg) {
      code = "invalid_request";
      message = String(detail[0].msg);
    }
  } catch {
    /* not JSON */
  }
  const header = Number(response.headers.get("Retry-After"));
  const retryAfter = Number.isFinite(header) && header > 0 ? header : null;
  let text = message ?? FALLBACK[response.status] ?? `The request failed (HTTP ${response.status}).`;
  if (retryAfter && (response.status === 429 || response.status === 503)) text += ` Try again in ${retryAfter} s.`;
  return new MemoryApiError(response.status, text, code, retryAfter);
}

export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>;

/** A new idempotency key for one add: a retry of the same add sends the same key and gets the same item. */
export function newIdempotencyKey(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
}

export function memoryClient(base: string = `${ROOT}/v1/memory`, fetcher: Fetcher = (i, init) => fetch(i, init)) {
  async function call<T>(path: string, init?: RequestInit): Promise<T> {
    let response: Response;
    try {
      response = await fetcher(base + path, {
        credentials: "same-origin",
        ...init,
        headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers },
      });
    } catch {
      throw new MemoryApiError(0, FALLBACK[0], "unreachable");
    }
    if (!response.ok) throw await memoryError(response);
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  return {
    list: () => call<MemoryList>(""),
    add: (text: string, idempotencyKey: string, kind: MemoryKind = "preference") =>
      call<MemoryItem>("", { method: "POST", body: JSON.stringify({ text, kind }), headers: { "Idempotency-Key": idempotencyKey } }),
    edit: (id: string, text: string) =>
      call<MemoryItem>(`/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ text }) }),
    remove: (id: string) => call<void>(`/${encodeURIComponent(id)}`, { method: "DELETE" }),
    forgetAll: () => call<void>("", { method: "DELETE" }),
    exportUrl: () => `${base}/export`,
  };
}

export type MemoryClient = ReturnType<typeof memoryClient>;

// ------------------------------------------------------------------ state
export interface MemoryState {
  status: "loading" | "ready" | "error";
  items: MemoryItem[];
  maxItems: number;
  retentionDays: number;
  error: string | null;
  /** Items hidden or changed optimistically, kept until the server confirms (for rollback). */
  pending: Record<string, MemoryItem>;
  /** The latest outcome, read out by screen readers (aria-live). */
  announcement: string;
}

export type MemoryAction =
  | { type: "loading" }
  | { type: "loaded"; list: MemoryList }
  | { type: "failed"; error: string }
  /** A change the service refused: the list stays as it is (never an error page). */
  | { type: "rejected"; error: string; announce: string }
  | { type: "added"; item: MemoryItem }
  | { type: "remove/start"; id: string }
  | { type: "remove/done"; id: string }
  | { type: "remove/undo"; id: string; error: string }
  | { type: "edit/start"; id: string; text: string }
  | { type: "edit/done"; item: MemoryItem }
  | { type: "edit/undo"; id: string; error: string }
  | { type: "cleared" }
  | { type: "announce"; message: string };

export const initialMemoryState: MemoryState = {
  status: "loading",
  items: [],
  maxItems: 0,
  retentionDays: 0,
  error: null,
  pending: {},
  announcement: "",
};

function newestFirst(items: MemoryItem[]): MemoryItem[] {
  return [...items].sort((a, b) => b.updated_at - a.updated_at);
}

function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  const { [key]: _drop, ...rest } = record;
  return rest;
}

export function memoryReducer(state: MemoryState, action: MemoryAction): MemoryState {
  switch (action.type) {
    case "loading":
      return { ...state, status: state.items.length ? state.status : "loading", error: null };
    case "loaded":
      return {
        ...state,
        status: "ready",
        items: newestFirst(action.list.items),
        maxItems: action.list.max_items,
        retentionDays: action.list.retention_days,
        error: null,
        pending: {},
      };
    case "failed": // loading the list failed: an error page only if there is nothing to show yet
      return { ...state, status: state.status === "loading" ? "error" : state.status, error: action.error };
    case "rejected":
      return { ...state, error: action.error, announcement: `${action.announce} ${action.error}` };
    case "added": {
      const rest = state.items.filter((i) => i.id !== action.item.id);
      return { ...state, items: newestFirst([action.item, ...rest]), error: null, announcement: "Remembered." };
    }
    case "remove/start": {
      const item = state.items.find((i) => i.id === action.id);
      if (!item) return state;
      return { ...state, items: state.items.filter((i) => i.id !== action.id), pending: { ...state.pending, [action.id]: item } };
    }
    case "remove/done":
      return { ...state, pending: without(state.pending, action.id), error: null, announcement: "Forgotten." };
    case "remove/undo": {
      const item = state.pending[action.id];
      if (!item) return { ...state, error: action.error };
      return {
        ...state,
        items: newestFirst([...state.items, item]),
        pending: without(state.pending, action.id),
        error: action.error,
        announcement: "Couldn't forget it. " + action.error,
      };
    }
    case "edit/start": {
      const item = state.items.find((i) => i.id === action.id);
      if (!item) return state;
      return {
        ...state,
        items: state.items.map((i) => (i.id === action.id ? { ...i, text: action.text } : i)),
        pending: { ...state.pending, [action.id]: item },
      };
    }
    case "edit/done":
      return {
        ...state,
        items: newestFirst(state.items.map((i) => (i.id === action.item.id ? action.item : i))),
        pending: without(state.pending, action.item.id),
        error: null,
        announcement: "Saved.",
      };
    case "edit/undo": {
      const item = state.pending[action.id];
      if (!item) return { ...state, error: action.error };
      return {
        ...state,
        items: newestFirst(state.items.map((i) => (i.id === action.id ? item : i))),
        pending: without(state.pending, action.id),
        error: action.error,
        announcement: "Couldn't save the change. " + action.error,
      };
    }
    case "cleared":
      return { ...state, items: [], pending: {}, error: null, announcement: "Everything was forgotten." };
    case "announce":
      return { ...state, announcement: action.message };
  }
}
