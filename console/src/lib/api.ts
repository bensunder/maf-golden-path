// Typed access to the service's own endpoints. Nothing here invents data: every value on screen comes
// from one of these calls, and a call that fails becomes an error state, never a placeholder number.

function meta(name: string, fallback: string): string {
  if (typeof document === "undefined") return fallback;
  const value = document.querySelector<HTMLMetaElement>(`meta[name="${name}"]`)?.content;
  return value && !value.startsWith("__") ? value : fallback;
}

export const API = meta("agentkit-api", "/v1/console").replace(/\/$/, "");
export const BASE = meta("agentkit-base", "/console/").replace(/\/$/, "");
/** Where this service is mounted when a router serves it under a path (e.g. "/agents/legal"); "" at the root. */
export const ROOT = meta("agentkit-root", "").replace(/\/$/, "");
/** Served by the VPS platform router: the server's agent list and Create agent are available. */
export const PLATFORM = meta("agentkit-platform", "") === "1";
/** "agent": this service's console. "fleet": the fleet service, one row per agent. */
export const MODE = meta("agentkit-mode", "agent") === "fleet" ? "fleet" : "agent";

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
  kind: "function" | "knowledge" | "connector";
  connector?: string;
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
  telemetry: { exporter: "app_insights" | "otlp" | null; capture_content: boolean; workbook_url: string | null; live_charts: boolean };
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

export type TrafficRange = "1h" | "24h" | "7d";

export interface TrafficPoint {
  t: string;
  runs: number;
  errors: number;
  blocked: number;
  avg_s: number | null;
  max_s: number | null;
  tokens: number;
}

export interface Traffic {
  available: boolean;
  range: TrafficRange;
  reason?: string;
  error?: string;
  bin_minutes?: number;
  queried_at?: number;
  series?: TrafficPoint[];
  totals?: { runs: number; errors: number; blocked: number; tokens: number; avg_s: number | null; max_s: number | null; error_rate: number | null; tool_calls: number };
  tools?: { tool: string; calls: number; failures: number; avg_ms: number | null; max_ms: number | null }[];
  recent?: { time: string; operation_id: string; duration_ms: number | null; success: boolean | null }[];
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
  // 502 carries a readable reason ({available:false, error}); surface it as data, not an exception.
  traffic: async (range: TrafficRange): Promise<Traffic> => {
    try {
      return await request<Traffic>(`${API}/traffic?range=${range}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 502) return { available: false, range, error: err.message };
      throw err;
    }
  },
  session: (id: string) => request<SessionInfo>(`${API}/sessions/${encodeURIComponent(id)}`),
  ready: () => request<{ status: string; agent: string; version: string }>(`${ROOT}/readyz`),
  decide: (sessionId: string, decisions: { id: string; approved: boolean; comment?: string }[]) =>
    request<ChatResult>(`${ROOT}/v1/sessions/${encodeURIComponent(sessionId)}/approvals`, {
      method: "POST",
      body: JSON.stringify({ decisions }),
    }),
  deleteSession: (sessionId: string) =>
    request<void>(`${ROOT}/v1/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" }),
};

// ------------------------------------------------------------------ fleet
export interface FleetAgent {
  url: string;
  console_url: string;
  source: "registry" | "discovered";
  name: string | null;
  status: "ready" | "not_ready" | "unreachable";
  console?: "ok" | "not_installed" | "denied" | "no_token" | "unreachable" | "error";
  detail: string | null;
  checked_at: number;
  overview: Pick<Overview, "service" | "agent" | "channels" | "knowledge" | "security" | "sessions" | "approvals" | "telemetry"> | null;
  gate: null | {
    cases: number;
    report: null | { passed: boolean; live: boolean; started_at: number; passed_runs: number; runs: number };
    error: string | null;
  };
}

export interface Fleet {
  agents: FleetAgent[];
  checked_at: number;
  discovery_error: string | null;
  registered: number;
  discovery: boolean;
}

export interface FleetTraffic {
  available: boolean;
  range: TrafficRange;
  reason?: string;
  error?: string;
  queried_at?: number;
  agents?: { agent: string; services: string[]; runs: number; errors: number; blocked: number; avg_s: number | null; tokens: number; error_rate: number | null }[];
}

export const fleetApi = {
  me: () => request<{ user: string | null; title: string; environment: string; kit_version: string | null }>(`${ROOT}/v1/fleet/me`),
  agents: () => request<Fleet>(`${ROOT}/v1/fleet/agents`),
  traffic: (range: TrafficRange) => request<FleetTraffic>(`${ROOT}/v1/fleet/traffic?range=${range}`),
};

// ------------------------------------------------------------------ platform (deploy/vps: create and launch agents)
// Served at the host's root by the platform router, so these paths are absolute (not under ROOT). On a
// service without the platform (Azure, a plain VPS stack) /v1/platform is a 404 and the console says so.
export interface PlatformInfo {
  platform: true;
  user: string;
  is_admin: boolean;
  admins_configured: boolean;
  public_host: string;
  fleet: boolean;
  agents_dir?: string;
  running_job: PlatformJob | null;
}

export interface PlatformAgent {
  name: string;
  title: string;
  path: string; // "" for the sample (served at the root), "/agents/<name>" otherwise
  internal: boolean;
  host?: string | null;
  service: string | null;
  created_by: string | null;
  created_at: string | null;
  status: "ready" | "not_ready" | "unreachable";
  version: string | null;
  connectors?: string[];
}

export type JobState = "queued" | "generating" | "registering" | "building" | "starting" | "ready" | "failed" | "removing" | "removed" | "connecting";

export interface PlatformJob {
  id: string;
  name: string;
  title: string;
  created_by: string;
  state: JobState;
  error: string | null;
  failed_step?: JobState | null;
  created_at: number;
  finished_at: number | null;
  path: string;
  log?: string[];
}

export interface NewAgent {
  title: string;
  name: string;
  description: string;
  team: string;
  knowledge: boolean;
  connectors: string[];
}

export const platformApi = {
  info: () => request<PlatformInfo>("/v1/platform"),
  agents: () => request<{ agents: PlatformAgent[]; jobs: PlatformJob[] }>("/v1/platform/agents"),
  create: (body: NewAgent) => request<PlatformJob>("/v1/platform/agents", { method: "POST", body: JSON.stringify(body) }),
  job: (id: string) => request<PlatformJob>(`/v1/platform/jobs/${encodeURIComponent(id)}`),
  remove: (name: string) =>
    request<PlatformJob>(`/v1/platform/agents/${encodeURIComponent(name)}`, { method: "DELETE", headers: { "X-Agentkit-Confirm": name } }),
};

// The same rules the platform enforces: what's typed lands in generated code.
export const AGENT_TITLE = /^[A-Za-z][A-Za-z0-9 .,()&+-]{1,58}[A-Za-z0-9).]$/;
export const AGENT_TEXT = /^[A-Za-z0-9 .,;:!?()&+/%#@-]{0,240}$/;
export const AGENT_TEAM = /^[a-z][a-z0-9-]{1,30}$/;
export const RESERVED_NAMES = ["agent", "auth", "redis", "fleet", "platform", "www", "api", "console", "chat", "v1", "sample"];

export function agentSlug(title: string): string {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/-{2,}/g, "-").replace(/^-+|-+$/g, "").slice(0, 32).replace(/-+$/, "");
}

// ------------------------------------------------------------------ connectors (platform): MCP servers agents may use
export interface ConnectorTool {
  name: string;
  description: string;
  read_only: boolean;
}

export interface Connector {
  name: string;
  title: string;
  url: string;
  host: string | null;
  auth: "none" | "bearer" | "header";
  header?: string | null;
  has_secret: boolean;
  tools: ConnectorTool[];
  allowed: string[];
  approval: string[];
  preset?: string | null;
  created_by: string | null;
  created_at: string | null;
  used_by: string[];
}

export interface ConnectorDraft {
  name: string;
  title: string;
  url: string;
  auth: Connector["auth"];
  header?: string;
  secret?: string;
  preset?: string;
  tools: ConnectorTool[];
  allowed: string[];
  approval: string[];
}

export interface ConnectorActivity {
  at: number;
  agent: string;
  connector: string;
  tool: string;
  allowed: boolean;
}

export const connectorsApi = {
  list: () => request<{ connectors: Connector[]; can_manage: boolean; vault: boolean }>("/v1/platform/connectors"),
  test: (body: { url?: string; auth?: string; header?: string; secret?: string; name?: string }) =>
    request<{ tools: ConnectorTool[] }>("/v1/platform/connectors/test", { method: "POST", body: JSON.stringify(body) }),
  add: (body: ConnectorDraft) => request<Connector>("/v1/platform/connectors", { method: "POST", body: JSON.stringify(body) }),
  update: (name: string, body: Partial<Pick<ConnectorDraft, "title" | "tools" | "allowed" | "approval" | "secret">>) =>
    request<Connector & { job: PlatformJob | null }>(`/v1/platform/connectors/${encodeURIComponent(name)}`, { method: "PATCH", body: JSON.stringify(body) }),
  remove: (name: string) =>
    request<{ removed: string }>(`/v1/platform/connectors/${encodeURIComponent(name)}`, { method: "DELETE", headers: { "X-Agentkit-Confirm": name } }),
  activity: (name: string) => request<{ activity: ConnectorActivity[] }>(`/v1/platform/connectors/${encodeURIComponent(name)}/activity`),
  assign: (agent: string, connectors: string[]) =>
    request<PlatformJob>(`/v1/platform/agents/${encodeURIComponent(agent)}/connectors`, { method: "PUT", body: JSON.stringify({ connectors }) }),
};

export const CONNECTOR_NAME = /^[a-z][a-z0-9_]{1,30}$/;

/** Services with a hosted MCP server that accepts a token (checked against each vendor's docs). */
export interface ConnectorPreset {
  id: string;
  title: string;
  url: string;
  auth: Connector["auth"];
  header?: string;
  secretLabel: string;
  help: string;
  docs: string;
}

export const CONNECTOR_PRESETS: ConnectorPreset[] = [
  { id: "linear", title: "Linear", url: "https://mcp.linear.app/mcp", auth: "bearer", secretLabel: "Linear API key",
    help: "Issues, projects and cycles. Create a personal API key in Linear: Settings → Security & access.", docs: "https://linear.app/docs/mcp" },
  { id: "github", title: "GitHub", url: "https://api.githubcopilot.com/mcp/", auth: "bearer", secretLabel: "GitHub personal access token",
    help: "Repositories, issues and pull requests. A fine-grained token scoped to the repositories the agent needs is safest.",
    docs: "https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp/set-up-the-github-mcp-server" },
  { id: "stripe", title: "Stripe", url: "https://mcp.stripe.com", auth: "bearer", secretLabel: "Stripe restricted API key",
    help: "Customers, payments and invoices. Use a restricted key with only the permissions the agent needs.", docs: "https://docs.stripe.com/mcp" },
  { id: "supabase", title: "Supabase", url: "https://mcp.supabase.com/mcp?project_ref=YOUR_PROJECT&read_only=true", auth: "bearer",
    secretLabel: "Supabase personal access token",
    help: "Your Postgres database and project. Replace YOUR_PROJECT with the project ref; keep read_only=true unless the agent must write.",
    docs: "https://supabase.com/docs/guides/ai-tools/mcp" },
];

/** Popular services whose hosted MCP servers need each person to sign in (OAuth): not available yet. */
export const OAUTH_ONLY = ["HubSpot", "Google Drive, Gmail and Calendar", "Salesforce", "Microsoft 365", "Notion (hosted)", "Slack"];
