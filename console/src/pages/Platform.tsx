import { ArrowDown, Check, CircleCheck, CircleX, FileCode, Loader2, Lock, MessageSquareText, Plus, Rocket, Terminal } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { EstimateNote, GoldenPathSummary } from "@/components/agent";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, Eyebrow } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/overlay";
import { Page, PageHeader, SectionTitle } from "@/components/ui/page";
import { ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, Tag } from "@/components/ui/status";
import { AGENT_TEAM, AGENT_TEXT, AGENT_TITLE, RESERVED_NAMES, agentSlug, platformApi, type PlatformInfo } from "@/lib/api";
import { STEPS, stepIndex, useJob, usePlatform } from "@/lib/platform";
import { useOverview } from "@/lib/data";
import { cn } from "@/lib/format";
import { Link, useRouter } from "@/lib/router";

// ------------------------------------------------------------------ platform / golden path
const TRADITIONAL = [
  "Authentication",
  "Security",
  "Guardrails",
  "Sessions",
  "Approvals",
  "Telemetry",
  "Knowledge",
  "Testing",
  "Evaluations",
  "Infrastructure",
  "CI/CD",
  "Channels",
];

export function PlatformPage() {
  const { navigate } = useRouter();
  return (
    <Page>
      <PageHeader
        title="The golden path"
        description="The developer builds the agent. The platform team owns the enterprise plumbing."
        actions={
          <Button size="sm" variant="primary" onClick={() => navigate("/agents/new")}>
            <Plus aria-hidden /> Create agent
          </Button>
        }
      />
      <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
        <div className="space-y-4">
          <Card>
            <CardHeader title="Traditional MAF development" description="Every team rebuilds the same enterprise plumbing before its first real feature." />
            <CardBody>
              <Eyebrow>Developer builds</Eyebrow>
              <ul className="mt-2.5 flex flex-wrap gap-1.5">
                {TRADITIONAL.map((t) => (
                  <li key={t} className="rounded-md border border-zinc-200 bg-zinc-50 px-2 py-1 text-[13px] text-zinc-500">
                    {t}
                  </li>
                ))}
              </ul>
            </CardBody>
          </Card>
          <div className="flex justify-center text-zinc-300" aria-hidden>
            <ArrowDown className="size-5" />
          </div>
          <Card>
            <CardHeader title="MAF Golden Path" description="The team writes what makes its agent different; everything else comes from the kit." />
            <CardBody>
              <GoldenPathSummary />
            </CardBody>
          </Card>
        </div>
        <div className="space-y-4">
          <Card className="p-6">
            <Eyebrow>Estimated enterprise platform work saved</Eyebrow>
            <div className="mt-3 text-3xl font-semibold tracking-tight text-zinc-950">28–46</div>
            <div className="text-sm font-medium text-zinc-900">engineer-days</div>
            <div className="text-[13px] text-zinc-500">per agent team</div>
            <EstimateNote className="mt-4" />
          </Card>
          <InlineNotice>
            Add-ons are estimated separately in the same document: Teams (5–8 days), web chat (3–6), and knowledge with permission trimming
            (6–10). Replace the estimates with your own teams' numbers.
          </InlineNotice>
        </div>
      </div>
    </Page>
  );
}

// ------------------------------------------------------------------ create agent
interface Capability {
  id: string;
  label: string;
  description: string;
  option?: "enable_web_chat" | "enable_teams" | "enable_knowledge";
}

const CAPABILITIES: Capability[] = [
  { id: "auth", label: "Enterprise authentication", description: "Entra sign-in with Easy Auth; managed identity to the model gateway." },
  { id: "guardrails", label: "Guardrails", description: "Prompt Shields on input and tool output, tool policy, run limits." },
  { id: "pii", label: "PII protection", description: "Redaction before the model and in stored history." },
  { id: "approvals", label: "Human approvals", description: "Pause risky tools for a person, with an audit trail." },
  { id: "sessions", label: "Durable sessions", description: "Cosmos DB sessions with cross-replica locking." },
  { id: "telemetry", label: "Telemetry", description: "OpenTelemetry to Application Insights; the platform dashboard and alerts." },
  { id: "evals", label: "Evaluations", description: "Eval cases that run offline in CI and live as the deploy gate." },
  { id: "knowledge", label: "Knowledge", description: "Azure AI Search, trimmed to each user's groups, with citations.", option: "enable_knowledge" },
  { id: "web", label: "Web Chat", description: "A chat page at /chat and an AG-UI endpoint.", option: "enable_web_chat" },
  { id: "teams", label: "Teams", description: "Azure Bot, approvals as Adaptive Cards.", option: "enable_teams" },
  { id: "azure", label: "Azure deployment", description: "Bicep, azd and an OIDC deploy pipeline." },
];

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

function shellQuote(value: string): string {
  return /^[A-Za-z0-9._@:/+-]+$/.test(value) ? value : `'${value.replace(/'/g, `'\\''`)}'`;
}

export function CreateAgentPage() {
  const { info, loading } = usePlatform();
  if (loading)
    return (
      <Page>
        <PageHeader eyebrow={<Crumb />} title="Create MAF Agent" />
        <LoadingRegion label="Loading" className="space-y-3">
          <Skeleton className="h-4 w-1/3" />
        </LoadingRegion>
      </Page>
    );
  return info ? <LaunchAgentPage info={info} /> : <GenerateAgentPage />;
}

// ------------------------------------------------------------------ create and launch (VPS platform)
function LaunchAgentPage({ info }: { info: PlatformInfo }) {
  const { search, navigate } = useRouter();
  const jobId = search.get("job");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [team, setTeam] = useState("");
  const [knowledge, setKnowledge] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const name = agentSlug(title);
  const teamValue = team.trim() || name;
  const issues = [
    title && !AGENT_TITLE.test(title.trim()) ? "Name: 3–60 letters, digits, spaces and . , ( ) & + -" : null,
    name && (name.length < 2 || !/^[a-z]/.test(name) || RESERVED_NAMES.includes(name)) ? `"${name}" can't be used as the agent's address` : null,
    description && !AGENT_TEXT.test(description) ? "Description: no quotes, braces or backslashes (it goes into the generated code)" : null,
    teamValue && !AGENT_TEAM.test(teamValue) ? "Team: lowercase letters, digits and dashes" : null,
  ].filter(Boolean) as string[];
  const valid = AGENT_TITLE.test(title.trim()) && issues.length === 0;

  if (jobId) return <LaunchProgress id={jobId} host={info.public_host} onAgain={() => navigate("/agents/new")} />;

  const submit = async () => {
    setSubmitting(true);
    setProblem(null);
    try {
      const job = await platformApi.create({ title: title.trim(), name, description: description.trim(), team: teamValue, knowledge });
      navigate(`/agents/new?job=${encodeURIComponent(job.id)}`);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "The agent couldn't be created.");
    } finally {
      setSubmitting(false);
    }
  };

  const input = "h-9 w-full rounded-md border border-zinc-200 px-3 text-sm outline-none focus:border-zinc-400 focus:ring-2 focus:ring-accent-500/20";
  return (
    <Page>
      <PageHeader
        eyebrow={<Crumb />}
        title="Create MAF Agent"
        description="Generate a governed Microsoft Agent Framework agent from the golden path, run its evals, and launch it on this server."
      />
      <form
        className="grid gap-6 lg:grid-cols-[1fr_320px]"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid && info.is_admin && !submitting) void submit();
        }}
      >
        <Card>
          <CardBody className="space-y-5">
            <Field id="agent-name" label="Agent name" hint={name ? <>It will live at <span className="font-mono">{info.public_host}/agents/{name}</span></> : "For example: Legal Desk"}>
              <input id="agent-name" value={title} required maxLength={60} onChange={(e) => setTitle(e.target.value)} aria-invalid={title.length > 0 && !valid} className={input} />
            </Field>
            <div className="grid gap-5 sm:grid-cols-2">
              <Field id="agent-desc" label="Description" hint="One sentence: what it helps with. Used in its charter and instructions.">
                <input id="agent-desc" value={description} maxLength={240} onChange={(e) => setDescription(e.target.value)} className={input} />
              </Field>
              <Field id="agent-team" label="Team" hint={<>Owning team (default: <span className="font-mono">{name || "the agent's name"}</span>).</>}>
                <input id="agent-team" value={team} maxLength={31} onChange={(e) => setTeam(e.target.value.toLowerCase())} className={input} />
              </Field>
            </div>
            <fieldset>
              <legend className="text-[13px] font-medium text-zinc-900">Included</legend>
              <ul className="mt-2 grid gap-1.5 text-[13px] text-zinc-600 sm:grid-cols-2">
                {["Microsoft Agent Framework agent", "Entra sign-in (this server's)", "Guardrails and PII redaction", "Tool policy and run limits",
                  "Human approval for risky tools", "Sessions (Redis on this server)", "Eval cases and the quality gate", "Web chat and this console"].map((t) => (
                  <li key={t} className="flex gap-2"><CircleCheck aria-hidden className="mt-0.5 size-4 shrink-0 text-emerald-600" /> {t}</li>
                ))}
              </ul>
              <label className="mt-4 flex cursor-pointer gap-3 rounded-md border border-zinc-200 px-3 py-2.5 hover:border-zinc-300">
                <input type="checkbox" checked={knowledge} onChange={(e) => setKnowledge(e.target.checked)} className="mt-0.5 size-4 shrink-0 accent-zinc-900" aria-describedby="knowledge-note" />
                <span>
                  <span className="text-sm text-zinc-900">Knowledge (company documents with citations)</span>
                  <span id="knowledge-note" className="mt-0.5 block text-xs text-zinc-500">Needs Azure AI Search. On this server the search tool says it can't search until one is connected.</span>
                </span>
              </label>
            </fieldset>
            {issues.length > 0 && <InlineNotice tone="warn">{issues.join(". ")}.</InlineNotice>}
            {problem && <InlineNotice tone="bad">{problem}</InlineNotice>}
            {!info.is_admin && (
              <InlineNotice tone="warn">
                {info.admins_configured
                  ? `Only platform admins can create agents on this server (you're signed in as ${info.user}).`
                  : "No platform admins are set up on this server yet: run agentctl.py platform --admins <your email> on the server."}
              </InlineNotice>
            )}
          </CardBody>
          <div className="flex items-center justify-end gap-3 border-t border-zinc-100 bg-zinc-50/50 px-5 py-3">
            {info.running_job && <span className="text-[13px] text-zinc-500">Another agent is being built: yours will start after it.</span>}
            <Button type="submit" variant="primary" disabled={!valid || !info.is_admin || submitting}>
              {submitting ? <Loader2 aria-hidden className="animate-spin" /> : <Rocket aria-hidden />} Create and launch
            </Button>
          </div>
        </Card>
        <div className="space-y-4">
          <Card className="p-5">
            <Eyebrow>What happens</Eyebrow>
            <ol className="mt-3 space-y-3 text-[13px] text-zinc-600">
              {STEPS.map((s, i) => (
                <li key={s.state} className="flex gap-2.5">
                  <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-zinc-100 text-2xs font-semibold text-zinc-600">{i + 1}</span>
                  <span><span className="font-medium text-zinc-800">{s.label}.</span> {s.detail}</span>
                </li>
              ))}
            </ol>
          </Card>
          <InlineNotice>
            Then make it yours: its tools, instructions and eval cases are in <span className="font-mono">{info.agents_dir ?? "/opt/agents"}/{name || "<name>"}</span> on the server.
            Put that folder in its own git repository; its CI runs the same evals on every change.
          </InlineNotice>
        </div>
      </form>
    </Page>
  );
}

function LaunchProgress({ id, host, onAgain }: { id: string; host: string; onAgain: () => void }) {
  const { job, error } = useJob(id);
  const log = useRef<HTMLPreElement>(null);
  const lines = job?.log?.length ?? 0;
  useEffect(() => {
    const el = log.current;
    if (el) el.scrollTop = el.scrollHeight; // follow the newest lines
  }, [lines]);
  if (error && !job) return <Page><PageHeader eyebrow={<Crumb />} title="Create MAF Agent" /><ErrorState title="Unable to follow this build" message={error.message} /></Page>;
  if (!job)
    return (
      <Page>
        <PageHeader eyebrow={<Crumb />} title="Create MAF Agent" />
        <LoadingRegion label="Loading" className="space-y-3"><Skeleton className="h-4 w-1/3" /></LoadingRegion>
      </Page>
    );
  const current = stepIndex(job.state);
  const failedAt = job.state === "failed" && job.failed_step ? STEPS.findIndex((s) => s.state === job.failed_step) : -1;
  return (
    <Page>
      <PageHeader
        eyebrow={<Crumb />}
        title={job.title}
        description={job.state === "ready" ? `Live at ${host}${job.path}` : job.state === "failed" ? "The agent couldn't be created. Nothing was left half-made." : "Creating your agent. This takes a few minutes; you can leave this page."}
        actions={
          job.state === "ready" ? (
            <div className="flex gap-2">
              <ButtonLinkPrimary href={`${job.path}/console`}>Open its console</ButtonLinkPrimary>
              <a href={`${job.path}/chat`} className="inline-flex h-8 items-center gap-1.5 rounded-md border border-zinc-200 bg-white px-3 text-[13px] font-medium text-zinc-900 hover:bg-zinc-50">
                <MessageSquareText aria-hidden className="size-4" /> Chat
              </a>
            </div>
          ) : job.state === "failed" ? (
            <Button size="sm" onClick={onAgain}>Try again</Button>
          ) : undefined
        }
      />
      <div className="grid gap-6 lg:grid-cols-[320px_1fr]">
        <Card className="p-5">
          <ol className="space-y-4" aria-label="Progress">
            {STEPS.map((s, i) => {
              const done = job.state === "ready" || i < current || (failedAt >= 0 && i < failedAt);
              const active = i === current && job.state !== "ready";
              const failed = i === failedAt;
              return (
                <li key={s.state} className="flex gap-3" aria-current={active ? "step" : undefined}>
                  <span className="mt-0.5">
                    {failed ? <CircleX aria-label="failed" className="size-5 text-red-600" />
                      : done ? <CircleCheck aria-label="done" className="size-5 text-emerald-600" />
                      : active ? <Loader2 aria-label="in progress" className="size-5 animate-spin text-accent-600 motion-reduce:animate-none" />
                      : <span aria-label="waiting" className="block size-5 rounded-full border-2 border-zinc-200" />}
                  </span>
                  <span>
                    <span className={cn("block text-sm", done || active ? "font-medium text-zinc-900" : "text-zinc-500")}>{s.label}</span>
                    <span className="block text-xs text-zinc-500">{s.detail}</span>
                  </span>
                </li>
              );
            })}
          </ol>
          <div className="mt-5 border-t border-zinc-100 pt-4">
            {job.state === "ready" ? <StatusBadge tone="ok">Ready</StatusBadge> : job.state === "failed" ? <StatusBadge tone="bad">Failed</StatusBadge> : <StatusBadge tone="pending">In progress</StatusBadge>}
            <p className="mt-2 text-xs text-zinc-500">Requested by {job.created_by}</p>
          </div>
        </Card>
        <Card>
          <CardHeader title="Build log" description={job.error ?? "The commands the platform runs, as they run."} />
          <pre ref={log} tabIndex={0} aria-label="Build log" className="max-h-[480px] overflow-auto bg-zinc-950 px-5 py-4 font-mono text-[12px] leading-relaxed text-zinc-100">
            {(job.log ?? []).slice(-120).join("\n") || "Waiting to start…"}
          </pre>
        </Card>
      </div>
    </Page>
  );
}

function ButtonLinkPrimary({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a href={href} className="inline-flex h-8 items-center gap-1.5 rounded-md bg-zinc-900 px-3 text-[13px] font-medium text-white hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 focus-visible:ring-offset-2">
      <Rocket aria-hidden className="size-4" /> {children}
    </a>
  );
}

// ------------------------------------------------------------------ generate a repository (no platform here)
function GenerateAgentPage() {
  const overview = useOverview();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [team, setTeam] = useState("");
  const [options, setOptions] = useState({ enable_web_chat: true, enable_teams: false, enable_knowledge: false });
  const [generated, setGenerated] = useState(false);
  const kit = overview.data?.service.kit_version;

  const slug = slugify(name);
  const valid = slug.length >= 3 && /^[a-z]/.test(slug);
  const command = useMemo(() => {
    const ref = kit ? `v${kit}` : "<version>";
    const data: [string, string][] = [
      ["project_name", name.trim()],
      ...(description.trim() ? ([["description", description.trim()]] as [string, string][]) : []),
      ...(team.trim() ? ([["team", team.trim()]] as [string, string][]) : []),
      ["enable_web_chat", String(options.enable_web_chat)],
      ["enable_teams", String(options.enable_teams)],
      ["enable_knowledge", String(options.enable_knowledge)],
    ];
    return [
      `copier copy --trust --vcs-ref ${ref}`,
      ...data.map(([k, v]) => `  --data ${k}=${shellQuote(v)}`),
      `  gh:bensunder/maf-golden-path ${slug || "<folder>"}`,
    ].join(" \\\n");
  }, [name, description, team, options, slug, kit]);

  const files = [
    { group: "Application", items: ["src/<package>/agent.py", "tools.py", "app.py", "instructions/system.md"] },
    { group: "Tests", items: ["tests/test_agent.py", "test_app.py", "test_evals.py", ...(options.enable_teams ? ["test_teams.py"] : []), ...(options.enable_knowledge ? ["test_knowledge.py"] : [])] },
    { group: "Evaluations", items: ["evals/cases.yaml"] },
    { group: "Infrastructure", items: ["infra/main.bicep", "azure.yaml", "Dockerfile"] },
    ...(options.enable_knowledge ? [{ group: "Knowledge", items: ["knowledge/", "acl.yaml"] }] : []),
    { group: "CI/CD", items: [".github/workflows/ (agent-ci, agent-deploy)"] },
  ];

  if (generated)
    return (
      <Page>
        <PageHeader
          eyebrow={<Crumb />}
          title="Your agent is ready to generate"
          description="Run this where you want the new repository. Generation happens on your machine; nothing is created on this server."
        />
        <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
          <div className="space-y-6">
            <Card>
              <CardHeader title="Generate" icon={<Terminal aria-hidden />} action={<CopyButton value={command} label="Copy command" />} />
              <pre className="overflow-x-auto bg-zinc-950 px-5 py-4 font-mono text-[12.5px] leading-relaxed text-zinc-100">{command}</pre>
              <CardBody className="text-[13px] text-zinc-600">
                Then <span className="font-mono">cd {slug}</span>, <span className="font-mono">make test</span> and{" "}
                <span className="font-mono">azd up</span>. Needs Python 3.11+ and <span className="font-mono">pipx install copier</span>.
              </CardBody>
            </Card>
            <Card>
              <CardHeader title="What you get" description="Governed by default: these come configured, with tests." />
              <CardBody className="grid gap-4 sm:grid-cols-2">
                {files.map((f) => (
                  <div key={f.group}>
                    <div className="flex items-center gap-2 text-sm font-medium text-zinc-900">
                      <CircleCheck aria-hidden className="size-4 text-emerald-600" /> {f.group}
                    </div>
                    <ul className="mt-1.5 space-y-0.5 pl-6">
                      {f.items.map((i) => (
                        <li key={i} className="font-mono text-xs text-zinc-500">
                          {i}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </CardBody>
            </Card>
          </div>
          <div className="space-y-4">
            <Card className="p-5">
              <Eyebrow>Your team writes</Eyebrow>
              <ul className="mt-3 space-y-2 text-sm text-zinc-800">
                <li className="flex gap-2">
                  <FileCode aria-hidden className="mt-0.5 size-4 text-accent-600" /> Tools in <span className="font-mono text-[13px]">tools.py</span>
                </li>
                <li className="flex gap-2">
                  <FileCode aria-hidden className="mt-0.5 size-4 text-accent-600" /> Instructions
                </li>
                <li className="flex gap-2">
                  <FileCode aria-hidden className="mt-0.5 size-4 text-accent-600" /> Eval cases
                </li>
              </ul>
            </Card>
            <Button onClick={() => setGenerated(false)}>Back to the form</Button>
          </div>
        </div>
      </Page>
    );

  return (
    <Page>
      <PageHeader eyebrow={<Crumb />} title="Create MAF Agent" description="Generate a governed enterprise agent in minutes." />
      <form
        className="grid gap-6 lg:grid-cols-[1fr_320px]"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) setGenerated(true);
        }}
      >
        <Card>
          <CardBody className="space-y-5">
            <Field id="agent-name" label="Agent name" hint={slug ? <>Repository and service: <span className="font-mono">{slug}</span></> : "For example: Order Status Agent"}>
              <input
                id="agent-name"
                value={name}
                required
                maxLength={60}
                onChange={(e) => setName(e.target.value)}
                aria-invalid={name.length > 0 && !valid}
                className="h-9 w-full rounded-md border border-zinc-200 px-3 text-sm outline-none focus:border-zinc-400 focus:ring-2 focus:ring-accent-500/20"
              />
            </Field>
            <div className="grid gap-5 sm:grid-cols-2">
              <Field id="agent-desc" label="Description" hint="One sentence, used in the charter and the agent card.">
                <input
                  id="agent-desc"
                  value={description}
                  maxLength={200}
                  onChange={(e) => setDescription(e.target.value)}
                  className="h-9 w-full rounded-md border border-zinc-200 px-3 text-sm outline-none focus:border-zinc-400 focus:ring-2 focus:ring-accent-500/20"
                />
              </Field>
              <Field id="agent-team" label="Team" hint="For quota and chargeback in the gateway.">
                <input
                  id="agent-team"
                  value={team}
                  maxLength={40}
                  onChange={(e) => setTeam(e.target.value)}
                  className="h-9 w-full rounded-md border border-zinc-200 px-3 text-sm outline-none focus:border-zinc-400 focus:ring-2 focus:ring-accent-500/20"
                />
              </Field>
            </div>

            <fieldset>
              <legend className="text-[13px] font-medium text-zinc-900">Capabilities</legend>
              <p className="mt-0.5 text-xs text-zinc-500">
                <Lock aria-hidden className="mr-1 inline size-3" />
                Included in every agent. Channels and knowledge are optional.
              </p>
              <ul className="mt-3 grid gap-2 sm:grid-cols-2">
                {CAPABILITIES.map((c) => {
                  const optional = Boolean(c.option);
                  const checked = c.option ? options[c.option] : true;
                  return (
                    <li key={c.id}>
                      <label
                        className={cn(
                          "flex h-full gap-3 rounded-md border px-3 py-2.5 transition-colors",
                          optional ? "cursor-pointer hover:border-zinc-300" : "cursor-default bg-zinc-50/60",
                          checked && optional ? "border-accent-300 bg-accent-50/40" : "border-zinc-200",
                        )}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={!optional}
                          onChange={(e) => c.option && setOptions((o) => ({ ...o, [c.option!]: e.target.checked }))}
                          className="mt-0.5 size-4 shrink-0 accent-zinc-900"
                          aria-describedby={`cap-${c.id}`}
                        />
                        <span className="min-w-0">
                          <span className="flex items-center gap-1.5 text-sm text-zinc-900">
                            {c.label}
                            {!optional && <Tag className="text-2xs">Always on</Tag>}
                          </span>
                          <span id={`cap-${c.id}`} className="mt-0.5 block text-xs leading-relaxed text-zinc-500">
                            {c.description}
                          </span>
                        </span>
                      </label>
                    </li>
                  );
                })}
              </ul>
            </fieldset>
          </CardBody>
          <div className="flex justify-end border-t border-zinc-100 bg-zinc-50/50 px-5 py-3">
            <Button type="submit" variant="primary" disabled={!valid}>
              Generate agent
            </Button>
          </div>
        </Card>
        <div className="space-y-4">
          <Card className="p-5">
            <Eyebrow>How it works</Eyebrow>
            <ol className="mt-3 space-y-3 text-[13px] text-zinc-600">
              {[
                "Copier generates a repository from the kit's template at this console's version.",
                "You add tools, instructions and eval cases.",
                "CI runs the tests and the offline gate; azd up deploys to the shared platform.",
              ].map((s, i) => (
                <li key={s} className="flex gap-2.5">
                  <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-zinc-100 text-2xs font-semibold text-zinc-600">{i + 1}</span>
                  {s}
                </li>
              ))}
            </ol>
          </Card>
          <SectionTitle>Version</SectionTitle>
          <p className="-mt-2 text-[13px] text-zinc-600">
            <Check aria-hidden className="mr-1 inline size-3.5 text-emerald-600" />
            Pinned to agentkit <span className="font-mono">{kit ?? "—"}</span>, the version serving this console.
          </p>
        </div>
      </form>
    </Page>
  );
}

function Crumb() {
  return (
    <nav aria-label="Breadcrumb" className="text-[13px] text-zinc-500">
      <Link to="/agents" className="hover:text-zinc-900">
        Agents
      </Link>
      <span aria-hidden className="px-1.5 text-zinc-300">
        /
      </span>
      <span aria-current="page">Create</span>
    </nav>
  );
}

function Field({ id, label, hint, children }: { id: string; label: string; hint?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="text-[13px] font-medium text-zinc-900">
        {label}
      </label>
      <div className="mt-1.5">{children}</div>
      {hint && <p className="mt-1 text-xs text-zinc-500">{hint}</p>}
    </div>
  );
}
