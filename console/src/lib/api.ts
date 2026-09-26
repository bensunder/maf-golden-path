// Typed access to the service's own endpoints. Nothing here invents data: every value on screen comes
// from one of these calls, and a call that fails becomes an error state, never a placeholder number.

function meta(name: string, fallback: string): string {
  if (typeof document === "undefined") return fallback;
  const value = document.querySelector<HTMLMetaElement>(`meta[name="${name}"]`)?.content;
  return value && !value.startsWith("__") ? value : fallback;
}

export const API = meta("agentkit-api", "/v1/console").replace(/\/$/, "");
export const BASE = meta("agentkit-base", "/console/").replace(/\/$/, "");

export type ControlStatus = "on" | "partial" | "off";

export interface Control {
  id: string;
  name: string;
  status: ControlStatus;
  detail: string;
}

export interface ToolInfo {
  name: string;
  description: string;
  approval: "never" | "always" | "rules";
  kind: "function" | "knowledge";
}

export interface Overview {
  service: {
    name: string;
    title: string;
    version: string;
    environment: "local" | "dev" | "test" | "prod";
    team: string;
    kit_version: string | null;
    maf_version: string | null;
    commit: string | null;
    run_url: string | null;
    deployed_at: string | null;
    started_at: number;
    hosting: { platform: string; app: string | null; revision: string | null; replica: string | null };
  };
  caller: { user: string | null; is_approver: boolean };
  agent: {
    name: string;
    description: string | null;
    model: string;
    gateway: boolean;
    auth_mode: string;
    tools: ToolInfo[];
    limits: {
      max_iterations: number;
      max_function_calls: number;
      max_run_seconds: number;
      session_token_budget: number;
      session_ttl_seconds: number;
      max_input_chars: number;
    };
  };
  channels: { id: string; name: string; path: string | null }[];
  knowledge: null | {
    tool: string;
    index: string;
    access: "groups" | "public";
    top_k: number;
    vector: boolean;
    embedding_model: string | null;
    semantic_ranker: boolean;
    search_configured: boolean;
    document_intelligence: boolean;
  };
  security: Control[];
  sessions: { store: "memory" | "redis" | "cosmos"; shared: boolean; ttl_seconds: number };
  approvals: { mode: "confirmation" | "approver" | "separation"; approver_role: string | null };
  telemetry: { exporter: "app_insights" | "otlp" | null; capture_content: boolean; workbook_url: string | null };
  links: { docs: string | null; chat: string | null };
}

export interface EvalCase {
  id: string;
  input: string;
  critical: boolean;
  as_user: boolean;
  scripted: boolean;
  checks: string[];
}

export interface GateCase {
  id: string;
  critical: boolean;
  runs: number;
  passed_runs: number;
  pass_rate: number;
  scores: Record<string, number>;
  failures: string[];
  skipped: string[];
  mean_duration_s?: number | null;
}

export interface GateReport {
  passed: boolean;
  reasons: string[];
  live: boolean;
  repeat: number;
  pass_rate: number;
  started_at: number;
  duration_s: number;
  baseline_used: boolean;
  cases: GateCase[];
}

export interface Evals {
  cases: EvalCase[] | null;
  cases_error: string | null;
  report: GateReport | null;
  report_error: string | null;
}

export interface PendingApproval {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
  requested_at: number | null;
}

export interface AuditEntry {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
  approved: boolean;
  decided_by: string | null;
  decided_by_name: string | null;
  channel: string | null;
  comment: string | null;
  requested_by: string | null;
  decided_at: number;
}

export interface SessionInfo {
  id: string;
  yours: boolean;
  expires_at: number;
  channel: string | null;
  pending: PendingApproval[];
  audit: AuditEntry[];
}

export interface ChatResult {
  session_id: string;
  status: "completed" | "approval_required";
  reply: string;
  blocked: string | null;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// Messages people can act on; the server's detail is used only when it is a plain sentence.
function friendly(status: number, detail: string | null): string {
  if (status === 401) return "You're not signed in, or your sign-in has expired. Reload the page to sign in again.";
  if (status === 403) return detail && detail.length < 160 ? capitalize(detail) + "." : "You don't have access to this.";
  if (status === 404) return detail && detail.length < 160 ? capitalize(detail) + "." : "Not found.";
  if (status === 409) return detail && detail.length < 200 ? capitalize(detail) + "." : "Someone else is working on this right now. Try again shortly.";
  if (status === 503) return "The agent is starting up. Try again in a few seconds.";
  if (status >= 500) return "The service couldn't complete the request.";
  return detail && detail.length < 200 ? capitalize(detail) + "." : `The request failed (HTTP ${status}).`;
}

function capitalize(s: string): string {
  const t = s.replace(/\.$/, "");
  return t.charAt(0).toUpperCase() + t.slice(1);
}

export async function errorFrom(response: Response): Promise<ApiError> {
  let detail: string | null = null;
  try {
    const data = await response.json();
    if (typeof data.detail === "string") detail = data.detail;
    else if (data.detail && typeof data.detail.detail === "string") detail = data.detail.detail;
  } catch {
    /* not JSON */
  }
  return new ApiError(response.status, friendly(response.status, detail));
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      credentials: "same-origin",
      ...init,
      headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers },
    });
  } catch {
    throw new ApiError(0, "The service can't be reached. Check your connection and try again.");
  }
  if (!response.ok) throw await errorFrom(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  overview: () => request<Overview>(`${API}/overview`),
  evals: () => request<Evals>(`${API}/evals`),
  session: (id: string) => request<SessionInfo>(`${API}/sessions/${encodeURIComponent(id)}`),
  ready: () => request<{ status: string; agent: string; version: string }>("/readyz"),
  decide: (sessionId: string, decisions: { id: string; approved: boolean; comment?: string }[]) =>
    request<ChatResult>(`/v1/sessions/${encodeURIComponent(sessionId)}/approvals`, {
      method: "POST",
      body: JSON.stringify({ decisions }),
    }),
  deleteSession: (sessionId: string) =>
    request<void>(`/v1/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" }),
};
