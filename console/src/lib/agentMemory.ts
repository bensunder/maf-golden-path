// The agent's own memory: lessons (reviewed), subject histories and the run journal, for memory admins.
// The typed client for /v1/memory/agent and a few pure helpers. No React here, so it is unit-tested directly.

import { memoryError, MemoryApiError, type Fetcher } from "./memory";

export type LessonStatus = "pending" | "active" | "archived";
export type LessonOrigin = "admin" | "user" | "untrusted";
export type RunTrigger = "user" | "agent" | "schedule" | "system";

export interface LessonRevision {
  action: "proposed" | "created" | "edited" | "approved" | "rejected" | "archived" | "restored" | "reverted";
  text: string;
  at: number;
  by: string;
}

export interface Lesson {
  id: string;
  text: string;
  status: LessonStatus;
  origin: LessonOrigin;
  created_at: number;
  updated_at: number;
  expires_at: number;
  run_id: string | null;
  history: LessonRevision[];
}

export interface LessonList {
  items: Lesson[];
  counts: Record<LessonStatus, number>;
}

export interface SubjectRef {
  subject: string;
  last_at: number;
}

export interface SubjectEvent {
  id: string;
  event: string;
  note: string;
  at: number;
  expires_at: number;
  run_id: string | null;
  trigger: RunTrigger;
  source: "agent" | "user";
}

export interface JournalEntry {
  run_id: string;
  at: number;
  duration_ms: number;
  trigger: RunTrigger;
  actor: string;
  outcome: "ok" | "blocked" | "error" | "awaiting_approval";
  blocked_reason: string | null;
  tools: string[];
  subjects: string[];
  lessons_proposed: number;
}

export interface JournalPage {
  entries: JournalEntry[];
  dropped: number;
  hours: number;
}

export interface AgentMemorySummary {
  is_admin: boolean;
  admins_configured: boolean;
  lessons: Record<LessonStatus, number> | null;
  config: {
    lesson_retention_days: number;
    lesson_review_days: number;
    max_lessons: number;
    subject_retention_days: number;
    max_subject_events: number;
    journal_retention_days: number;
  };
}

/** The overview's description of the agent's own memory (``memory.agent``). */
export interface AgentMemoryInfo {
  store: string;
  salt_configured: boolean;
  admins_configured: boolean;
  lesson_retention_days: number;
  lesson_review_days: number;
  max_lessons: number;
  subject_retention_days: number;
  max_subject_events: number;
  journal_retention_days: number;
  tools: string[];
}

/** Same shape the service enforces (models.SUBJECT_PATTERN), so a typo is caught before the request. */
export const SUBJECT_PATTERN = /^[a-z][a-z0-9_]{0,31}:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export function agentMemoryClient(memoryBase: string, fetcher: Fetcher = (i, init) => fetch(i, init)) {
  const base = `${memoryBase.replace(/\/$/, "")}/agent`;

  async function call<T>(path: string, init?: RequestInit): Promise<T> {
    let response: Response;
    try {
      response = await fetcher(base + path, {
        credentials: "same-origin",
        ...init,
        // Every POST carries a JSON body (the service refuses anything else: its CSRF defence).
        headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers },
      });
    } catch {
      throw new MemoryApiError(0, "The service can't be reached. Check your connection and try again.", "unreachable");
    }
    if (!response.ok) throw await memoryError(response);
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  const lesson = (id: string) => `/lessons/${encodeURIComponent(id)}`;
  const post = (body: unknown = {}) => ({ method: "POST", body: JSON.stringify(body) });

  return {
    summary: () => call<AgentMemorySummary>(""),
    lessons: () => call<LessonList>("/lessons"),
    addLesson: (text: string) => call<Lesson>("/lessons", post({ text })),
    editLesson: (id: string, text: string) => call<Lesson>(lesson(id), { method: "PATCH", body: JSON.stringify({ text }) }),
    approve: (id: string) => call<Lesson>(`${lesson(id)}/approve`, post()),
    reject: (id: string) => call<Lesson>(`${lesson(id)}/reject`, post()),
    restore: (id: string) => call<Lesson>(`${lesson(id)}/restore`, post()),
    revert: (id: string, revision: number) => call<Lesson>(`${lesson(id)}/revert`, post({ revision })),
    archive: (id: string) => call<void>(lesson(id), { method: "DELETE" }),
    subjects: () => call<{ items: SubjectRef[] }>("/subjects"),
    subject: (subject: string) => call<{ subject: string; events: SubjectEvent[] }>(`/subjects/${encodeURIComponent(subject)}`),
    forgetSubject: (subject: string) => call<void>(`/subjects/${encodeURIComponent(subject)}`, { method: "DELETE" }),
    journal: (hours = 24, subject?: string) => {
      const q = new URLSearchParams({ hours: String(hours) });
      if (subject) q.set("subject", subject);
      return call<JournalPage>(`/journal?${q.toString()}`);
    },
  };
}

export type AgentMemoryClient = ReturnType<typeof agentMemoryClient>;

// ------------------------------------------------------------------ helpers
export const ORIGIN_LABEL: Record<LessonOrigin, string> = {
  admin: "Stated by an admin",
  user: "Stated by a user",
  untrusted: "Learned from outside data",
};

export const TRIGGER_LABEL: Record<RunTrigger, string> = {
  user: "Person",
  agent: "Another agent",
  schedule: "Schedule",
  system: "Unattended",
};

export const OUTCOME_TONE: Record<JournalEntry["outcome"], "ok" | "warn" | "bad" | "info"> = {
  ok: "ok",
  awaiting_approval: "info",
  blocked: "warn",
  error: "bad",
};

/** Lessons in one status, most recently updated first. */
export function lessonsIn(list: Lesson[], status: LessonStatus): Lesson[] {
  return list.filter((l) => l.status === status).sort((a, b) => b.updated_at - a.updated_at);
}

/** Replace (or add) one lesson in a list, as returned by the service after a change. */
export function upsertLesson(list: Lesson[], lesson: Lesson): Lesson[] {
  return [lesson, ...list.filter((l) => l.id !== lesson.id)];
}

/** Earlier texts a lesson can be rolled back to: each distinct text in its history except the current one,
 *  newest first, with the revision index the service expects. */
export function revertOptions(lesson: Lesson): { revision: number; text: string; at: number }[] {
  const seen = new Set([lesson.text]);
  const out: { revision: number; text: string; at: number }[] = [];
  for (let i = lesson.history.length - 1; i >= 0; i--) {
    const h = lesson.history[i];
    if (!seen.has(h.text)) {
      seen.add(h.text);
      out.push({ revision: i, text: h.text, at: h.at });
    }
  }
  return out;
}

export function humanizeEvent(event: string): string {
  const words = event.replace(/_/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : event;
}
