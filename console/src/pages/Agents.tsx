import { ExternalLink, Loader2, MessageSquareText, Plug, Plus, Shield, Trash2 } from "lucide-react";
import { useState } from "react";

import { HealthDot, MoreLink, PostureList, RuntimeGrid, ToolList, useHealth } from "@/components/agent";
import { Button, ButtonLink } from "@/components/ui/button";
import { Card, CardBody, CardHeader, KeyValue } from "@/components/ui/card";
import { Page, PageHeader } from "@/components/ui/page";
import { EmptyState, ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, Tag } from "@/components/ui/status";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { ROOT, connectorsApi, platformApi, type Overview, type PlatformAgent, type PlatformInfo, type PlatformJob } from "@/lib/api";
import { Dialog } from "@/components/ui/overlay";
import { useJob } from "@/lib/platform";
import { useLoad } from "@/lib/data";
import { useOverview } from "@/lib/data";
import { usePlatform, usePlatformAgents } from "@/lib/platform";
import { duration, environmentLabel, number } from "@/lib/format";
import { Link, useRouter } from "@/lib/router";

// ------------------------------------------------------------------ list
export function AgentsPage() {
  const { info, loading } = usePlatform();
  if (loading) return <Page><PageHeader title="Agents" /><LoadingRegion label="Loading agents" className="space-y-3"><Skeleton className="h-4 w-1/3" /></LoadingRegion></Page>;
  return info ? <PlatformAgentsPage info={info} /> : <ServiceAgentsPage />;
}

// ------------------------------------------------------------------ every agent on the server (VPS platform)
const JOB_LABEL: Partial<Record<PlatformJob["state"], string>> = {
  queued: "Queued", generating: "Generating", registering: "Registering", building: "Building and running evals",
  starting: "Starting", removing: "Removing", connecting: "Updating connectors",
};

function PlatformAgentsPage({ info }: { info: PlatformInfo }) {
  const { data, error, reload } = usePlatformAgents(true);
  const { navigate } = useRouter();
  const [removing, setRemoving] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [editing, setEditing] = useState<PlatformAgent | null>(null);

  const remove = async (a: PlatformAgent) => {
    if (!window.confirm(`Remove ${a.title}? It stops and leaves this server. Its files are kept on the server (in .removed).`)) return;
    setRemoving(a.name);
    setProblem(null);
    try {
      await platformApi.remove(a.name);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "The agent couldn't be removed.");
    } finally {
      setRemoving(null);
      reload();
    }
  };

  return (
    <Page wide>
      <PageHeader
        title="Agents"
        description="Every MAF agent on this server. Create agent generates a new one from the golden-path template, runs its evals, builds and starts it here."
        actions={
          <Button size="sm" variant="primary" onClick={() => navigate("/agents/new")}>
            <Plus aria-hidden /> Create agent
          </Button>
        }
      />
      {problem && <InlineNotice tone="bad" className="mb-4">{problem}</InlineNotice>}
      <Card>
        {error && !data ? (
          <ErrorState title="Unable to load the agents on this server" message={error.message} onRetry={reload} />
        ) : !data ? (
          <LoadingRegion label="Loading agents" className="space-y-3 p-5">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-4 w-1/2" />
          </LoadingRegion>
        ) : (
          <Table label="Agents on this server">
            <thead>
              <tr>
                <Th>Agent</Th>
                <Th>Status</Th>
                <Th className="hidden md:table-cell">Version</Th>
                <Th className="hidden lg:table-cell">Created by</Th>
                <Th className="hidden md:table-cell">Connectors</Th>
                <Th className="text-right">Open</Th>
              </tr>
            </thead>
            <tbody>
              {data.jobs.map((j) => (
                <Tr key={j.id}>
                  <Td>
                    <div className="font-medium text-zinc-950">{j.title}</div>
                    <div className="font-mono text-xs text-zinc-500">{j.path}</div>
                  </Td>
                  <Td>
                    <span className="inline-flex items-center gap-1.5 text-[13px] text-accent-700">
                      <Loader2 aria-hidden className="size-3.5 animate-spin motion-reduce:animate-none" />
                      {JOB_LABEL[j.state] ?? j.state}
                    </span>
                  </Td>
                  <Td className="hidden md:table-cell">—</Td>
                  <Td className="hidden lg:table-cell text-[13px]">{j.created_by}</Td>
                  <Td className="hidden md:table-cell">—</Td>
                  <Td className="text-right">
                    <Link to={`/agents/new?job=${encodeURIComponent(j.id)}`} className="text-[13px] text-accent-700 hover:underline">
                      Progress
                    </Link>
                  </Td>
                </Tr>
              ))}
              {data.agents.map((a) => {
                const here = a.path === ROOT;
                const base = a.internal || !a.host ? a.path : `https://${a.host}`;
                return (
                  <Tr key={a.name}>
                    <Td>
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-zinc-950">{a.title}</span>
                        {here && <Tag>This console</Tag>}
                      </div>
                      <div className="font-mono text-xs text-zinc-500">{info.public_host}{a.path || "/"}</div>
                    </Td>
                    <Td>
                      <StatusBadge tone={a.status === "ready" ? "ok" : a.status === "not_ready" ? "warn" : "bad"}>
                        {a.status === "ready" ? "Ready" : a.status === "not_ready" ? "Not ready" : "Unreachable"}
                      </StatusBadge>
                    </Td>
                    <Td className="hidden font-mono text-[13px] md:table-cell">{a.version ?? "—"}</Td>
                    <Td className="hidden lg:table-cell text-[13px] text-zinc-600">{a.created_by ?? (a.name === "sample" ? "Sample" : "—")}</Td>
                    <Td className="hidden md:table-cell">
                      <div className="flex flex-wrap items-center gap-1">
                        {(a.connectors ?? []).map((c) => <Tag key={c}>{c}</Tag>)}
                        {info.is_admin && (
                          <Button size="sm" variant="ghost" onClick={() => setEditing(a)} aria-label={`Choose connectors for ${a.title}`}>
                            <Plug aria-hidden /> {(a.connectors ?? []).length ? "Edit" : "Add"}
                          </Button>
                        )}
                        {!info.is_admin && !(a.connectors ?? []).length && <span className="text-[13px] text-zinc-400">None</span>}
                      </div>
                    </Td>
                    <Td className="text-right">
                      <div className="inline-flex items-center gap-1.5">
                        <ButtonLink size="sm" href={`${base}/console`}>Console</ButtonLink>
                        <ButtonLink size="sm" href={`${base}/chat`}>
                          <MessageSquareText aria-hidden className="mr-1" /> Chat
                        </ButtonLink>
                        {info.is_admin && a.internal && a.name !== "sample" && !here && (
                          <Button size="sm" onClick={() => void remove(a)} disabled={removing !== null} aria-label={`Remove ${a.title}`}>
                            {removing === a.name ? <Loader2 aria-hidden className="animate-spin" /> : <Trash2 aria-hidden />}
                          </Button>
                        )}
                      </div>
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
      {editing && <ConnectorsDialog agent={editing} onClose={(changed) => { setEditing(null); if (changed) reload(); }} />}
      <InlineNotice className="mt-4">
        {info.is_admin
          ? "You're a platform admin: you can create and remove agents here. Each agent is a Microsoft Agent Framework service with the golden path's guardrails, approvals, sessions, evals and console."
          : `Signed in as ${info.user}. Only platform admins can create or remove agents on this server; anyone signed in can open them.`}
        {info.fleet && (
          <>
            {" "}
            <a href="/fleet/console" className="font-medium text-accent-700 hover:underline">Open the fleet view</a> for security posture and quality gates side by side.
          </>
        )}
      </InlineNotice>
    </Page>
  );
}

function ConnectorsDialog({ agent, onClose }: { agent: PlatformAgent; onClose: (changed: boolean) => void }) {
  const catalog = useLoad(connectorsApi.list);
  const [chosen, setChosen] = useState<Set<string>>(new Set(agent.connectors ?? []));
  const [jobId, setJobId] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const { job } = useJob(jobId);
  const done = job?.state === "ready" || job?.state === "failed";
  const save = async () => {
    setProblem(null);
    try {
      setJobId((await connectorsApi.assign(agent.name, [...chosen])).id);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "The connectors couldn't be changed.");
    }
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && onClose(Boolean(jobId))}
      title={`Connectors for ${agent.title}`}
      description="The agent restarts with the new set (no rebuild). Write tools still ask the person in the chat before they run."
      footer={
        jobId ? (
          <Button variant="primary" disabled={!done} onClick={() => onClose(true)}>{done ? "Close" : <><Loader2 aria-hidden className="animate-spin" /> Restarting…</>}</Button>
        ) : (
          <>
            <Button onClick={() => onClose(false)}>Cancel</Button>
            <Button variant="primary" onClick={() => void save()} disabled={!catalog.data}>Save and restart</Button>
          </>
        )
      }
    >
      {catalog.error ? (
        <InlineNotice tone="bad">{catalog.error.message}</InlineNotice>
      ) : !catalog.data ? (
        <Skeleton className="h-4 w-1/2" />
      ) : catalog.data.connectors.length === 0 ? (
        <p className="text-[13px] text-zinc-600">No connectors on this server yet. <Link to="/connectors/new" className="font-medium text-accent-700 hover:underline">Add one</Link> first.</p>
      ) : (
        <ul className="space-y-2">
          {catalog.data.connectors.map((c) => (
            <li key={c.name}>
              <label className="flex cursor-pointer gap-3 rounded-md border border-zinc-200 px-3 py-2 hover:border-zinc-300">
                <input type="checkbox" className="mt-0.5 size-4 accent-zinc-900" checked={chosen.has(c.name)} disabled={Boolean(jobId)}
                  onChange={(e) => setChosen((s) => { const n = new Set(s); if (e.target.checked) n.add(c.name); else n.delete(c.name); return n; })} />
                <span>
                  <span className="block text-sm text-zinc-900">{c.title}</span>
                  <span className="block text-xs text-zinc-500">{c.allowed.length} tools{c.approval.length ? `, ${c.approval.length} need approval` : ""} · {c.host}</span>
                </span>
              </label>
            </li>
          ))}
        </ul>
      )}
      {problem && <InlineNotice tone="bad" className="mt-3">{problem}</InlineNotice>}
      {job && (
        <InlineNotice tone={job.state === "failed" ? "bad" : "info"} className="mt-3">
          {job.state === "ready" ? "Done: the agent is running with its new connectors." : job.state === "failed" ? `It didn't work: ${job.error}` : "Updating its settings and restarting it…"}
        </InlineNotice>
      )}
    </Dialog>
  );
}

// ------------------------------------------------------------------ this service's agent (no platform)
function ServiceAgentsPage() {
  const { data, error, reload } = useOverview();
  const { health } = useHealth();
  const { navigate } = useRouter();
  return (
    <Page>
      <PageHeader
        title="Agents"
        description="Governed agents served by this service."
        actions={
          <Button size="sm" variant="primary" onClick={() => navigate("/agents/new")}>
            <Plus aria-hidden /> Create agent
          </Button>
        }
      />
      <Card>
        {error && !data ? (
          <ErrorState title="Unable to load agents" message={error.message} onRetry={reload} />
        ) : !data ? (
          <LoadingRegion label="Loading agents" className="space-y-3 p-5">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-4 w-1/2" />
          </LoadingRegion>
        ) : (
          <Table label="Agents">
            <thead>
              <tr>
                <Th>Agent</Th>
                <Th>Status</Th>
                <Th className="hidden md:table-cell">Version</Th>
                <Th className="hidden md:table-cell">Model</Th>
                <Th className="hidden lg:table-cell">Environment</Th>
                <Th className="hidden lg:table-cell">Hosting</Th>
              </tr>
            </thead>
            <tbody>
              <Tr interactive onClick={() => navigate(`/agents/${encodeURIComponent(data.agent.name)}`)}>
                <Td>
                  <Link to={`/agents/${encodeURIComponent(data.agent.name)}`} className="font-medium text-zinc-950 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500">
                    {data.service.title}
                  </Link>
                  <div className="font-mono text-xs text-zinc-500">{data.agent.name}</div>
                </Td>
                <Td>
                  <HealthDot health={health} />
                </Td>
                <Td className="hidden font-mono text-[13px] md:table-cell">{data.service.version}</Td>
                <Td className="hidden font-mono text-[13px] md:table-cell">{data.agent.model}</Td>
                <Td className="hidden lg:table-cell">{environmentLabel(data.service.environment)}</Td>
                <Td className="hidden lg:table-cell">{data.service.hosting.platform}</Td>
              </Tr>
            </tbody>
          </Table>
        )}
      </Card>
      <InlineNotice className="mt-4">
        Each agent on the golden path is its own service with its own console. Agents in other services don't appear here.
      </InlineNotice>
    </Page>
  );
}

// ------------------------------------------------------------------ detail
export function AgentDetailPage({ name }: { name: string }) {
  const { data, error, reload } = useOverview();
  if (error && !data)
    return (
      <Page>
        <Card>
          <ErrorState title="Unable to load the agent" message={error.message} onRetry={reload} />
        </Card>
      </Page>
    );
  if (!data)
    return (
      <Page>
        <LoadingRegion label="Loading agent" className="space-y-6">
          <Skeleton className="h-7 w-64" />
          <Skeleton className="h-4 w-80" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-64 w-full" />
        </LoadingRegion>
      </Page>
    );
  if (data.agent.name !== name)
    return (
      <Page>
        <Card>
          <EmptyState title="Agent not found" action={<MoreLink to="/agents">All agents</MoreLink>}>
            This service runs <span className="font-mono">{data.agent.name}</span>. Other agents have their own console.
          </EmptyState>
        </Card>
      </Page>
    );
  return <AgentDetail data={data} />;
}

function AgentDetail({ data }: { data: Overview }) {
  const { health } = useHealth();
  const { navigate } = useRouter();
  const { agent, service } = data;
  return (
    <Page>
      <PageHeader
        eyebrow={
          <nav aria-label="Breadcrumb" className="text-[13px] text-zinc-500">
            <Link to="/agents" className="hover:text-zinc-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500">
              Agents
            </Link>
            <span aria-hidden className="px-1.5 text-zinc-300">/</span>
            <span aria-current="page">{service.title}</span>
          </nav>
        }
        title={service.title}
        description={agent.description ?? undefined}
        meta={
          <>
            <HealthDot health={health} />
            <Tag mono>v{service.version}</Tag>
            <span className="text-[13px] text-zinc-500">{service.hosting.platform}</span>
            <span aria-hidden className="text-zinc-300">·</span>
            <span className="text-[13px] text-zinc-500">{environmentLabel(service.environment)}</span>
            <span aria-hidden className="text-zinc-300">·</span>
            <span className="text-[13px] text-zinc-500">Team {service.team}</span>
          </>
        }
        actions={
          <>
            {data.links.chat && (
              <ButtonLink size="sm" href={data.links.chat} external>
                <ExternalLink aria-hidden /> Web chat
              </ButtonLink>
            )}
            <Button size="sm" variant="primary" onClick={() => navigate("/playground")}>
              <MessageSquareText aria-hidden /> Open playground
            </Button>
          </>
        }
      />

      <section aria-labelledby="runtime-title" className="mb-6">
        <h2 id="runtime-title" className="sr-only">
          Runtime
        </h2>
        <RuntimeGrid data={data} />
      </section>

      <div className="grid gap-6 xl:grid-cols-[1.35fr_1fr]">
        <div className="space-y-6">
          <Card>
            <CardHeader title="Tools" description="What the agent can do, and which actions wait for a person." />
            {agent.tools.length ? (
              <ToolList tools={agent.tools} />
            ) : (
              <EmptyState compact title="No tools">
                This agent only answers from its instructions.
              </EmptyState>
            )}
          </Card>

          <Card>
            <CardHeader title="Run limits" description="Enforced on every run, whatever the model decides." />
            <CardBody>
              <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3">
                <KeyValue label="Model calls per run">{agent.limits.max_iterations}</KeyValue>
                <KeyValue label="Tool calls per run">{agent.limits.max_function_calls}</KeyValue>
                <KeyValue label="Run timeout">{duration(agent.limits.max_run_seconds)}</KeyValue>
                <KeyValue label="Tokens per session">{number(agent.limits.session_token_budget)}</KeyValue>
                <KeyValue label="Session lifetime">{duration(agent.limits.session_ttl_seconds)}</KeyValue>
                <KeyValue label="Max input">{number(agent.limits.max_input_chars)} chars</KeyValue>
              </dl>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Channels" description="Where users reach this agent. All share sessions, approvals and audit." />
            <ul className="divide-y divide-zinc-100">
              {data.channels.map((c) => (
                <li key={c.id} className="flex items-center justify-between gap-4 px-5 py-2.5">
                  <span className="text-sm text-zinc-900">{c.name}</span>
                  {c.path && <span className="font-mono text-xs text-zinc-500">{c.path}</span>}
                </li>
              ))}
            </ul>
          </Card>
        </div>

        <div className="space-y-6">
          <Card>
            <CardHeader title="Security posture" icon={<Shield aria-hidden />} action={<MoreLink to="/security">Details</MoreLink>} />
            <CardBody className="py-1">
              <PostureList controls={data.security} compact />
            </CardBody>
          </Card>
          <Card>
            <CardHeader title="Governance" />
            <CardBody>
              <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-1">
                <KeyValue label="Approvals">
                  {data.approvals.mode === "confirmation"
                    ? "The requester confirms"
                    : data.approvals.mode === "separation"
                      ? `Approver role ${data.approvals.approver_role}, never the requester`
                      : `Approver role ${data.approvals.approver_role}`}
                </KeyValue>
                <KeyValue label="Sessions">
                  {data.sessions.shared ? `Shared (${data.sessions.store})` : "In memory (single replica)"}
                </KeyValue>
                <KeyValue label="Model access">
                  {agent.gateway ? "AI gateway" : "Direct"} · <span className="font-mono text-[13px]">{agent.auth_mode}</span>
                </KeyValue>
                <KeyValue label="Telemetry">
                  {data.telemetry.exporter === "app_insights" ? (
                    "Application Insights"
                  ) : data.telemetry.exporter === "otlp" ? (
                    "OTLP"
                  ) : (
                    <StatusBadge tone="neutral">Not connected</StatusBadge>
                  )}
                </KeyValue>
              </dl>
            </CardBody>
          </Card>
        </div>
      </div>
    </Page>
  );
}
