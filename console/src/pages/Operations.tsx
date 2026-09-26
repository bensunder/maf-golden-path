// Knowledge, Telemetry, Security, Deployments and Settings: views of the running service's configuration.
import { Activity, ExternalLink, GitCommitHorizontal, Library, Lock, Rocket, Shield, Terminal } from "lucide-react";
import type { ReactNode } from "react";

import { HealthDot, MoreLink, PostureList, postureSummary, useHealth } from "@/components/agent";
import { ButtonLink } from "@/components/ui/button";
import { Card, CardBody, CardHeader, KeyValue } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/overlay";
import { Page, PageHeader, SectionTitle } from "@/components/ui/page";
import { EmptyState, ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, StatusDot, Tag } from "@/components/ui/status";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { api, type Overview } from "@/lib/api";
import { safeUrl } from "@/lib/agui";
import { useLoad, useOverview } from "@/lib/data";
import { dateTime, duration, environmentLabel, number, timeAgo } from "@/lib/format";

/** Renders the page body once the overview is loaded, with shared loading and error states. */
function WithOverview({ title, children }: { title: string; children: (data: Overview) => ReactNode }) {
  const { data, error, reload } = useOverview();
  if (error && !data)
    return (
      <Card>
        <ErrorState title={`Unable to load ${title.toLowerCase()}`} message={error.message} onRetry={reload} />
      </Card>
    );
  if (!data)
    return (
      <LoadingRegion label={`Loading ${title.toLowerCase()}`} className="space-y-4">
        <Card className="space-y-3 p-6">
          <Skeleton className="h-4 w-40" />
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </Card>
      </LoadingRegion>
    );
  return <>{children(data)}</>;
}

// ------------------------------------------------------------------ knowledge
export function KnowledgePage() {
  return (
    <Page>
      <PageHeader title="Knowledge" description="Manage the enterprise knowledge available to agents." />
      <WithOverview title="Knowledge">{(data) => (data.knowledge ? <KnowledgeBody data={data} /> : <NoKnowledge />)}</WithOverview>
    </Page>
  );
}

function NoKnowledge() {
  return (
    <Card>
      <EmptyState icon={<Library aria-hidden />} title="No knowledge connected" action={<MoreLink to="/agents/new">See the knowledge option</MoreLink>}>
        This agent doesn't search company documents. Generate the service with <span className="font-mono">enable_knowledge</span> to add
        Azure AI Search with per-user permission trimming and citations.
      </EmptyState>
    </Card>
  );
}

function KnowledgeBody({ data }: { data: Overview }) {
  const k = data.knowledge!;
  const ingest = "agentkit-ingest --source knowledge/ --acl knowledge/acl.yaml";
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader title="Sources" description="As configured in this service. The service doesn't probe Search on page load." />
        <ul className="divide-y divide-zinc-100">
          <SourceRow name="Azure AI Search" detail={<>Index <span className="font-mono">{k.index}</span> · top {k.top_k} passages</>}>
            {k.search_configured ? <StatusDot tone="ok">Configured</StatusDot> : <StatusDot tone="warn">No endpoint set</StatusDot>}
          </SourceRow>
          <SourceRow
            name="Retrieval"
            detail={[k.vector ? `Hybrid (keyword + vector, ${k.embedding_model})` : "Keyword only", k.semantic_ranker ? "semantic ranker" : null].filter(Boolean).join(" · ")}
          >
            <StatusDot tone="ok">Configured</StatusDot>
          </SourceRow>
          <SourceRow name="Document Intelligence" detail="Extracts PDF and Office files during ingestion">
            {k.document_intelligence ? <StatusDot tone="ok">Configured</StatusDot> : <StatusDot tone="neutral">Not configured</StatusDot>}
          </SourceRow>
          <SourceRow name="Agent tool" detail="What the agent calls to search">
            <span className="font-mono text-[13px] text-zinc-700">{k.tool}</span>
          </SourceRow>
        </ul>
      </Card>

      <Card>
        <CardHeader title="Permissions" description="Who can read what is enforced in the search query itself, per user." icon={<Lock aria-hidden />} />
        <CardBody>
          <ul className="grid gap-4 sm:grid-cols-2">
            <Perm title="Entra groups" on={k.access === "groups"}>
              {k.access === "groups"
                ? "Each search is trimmed to the caller's Entra groups, including nested groups."
                : "This index is public to every user of the agent (set explicitly)."}
            </Perm>
            <Perm title="ACL filtering" on={k.access === "groups"}>
              Every chunk carries the groups allowed to read it, from <span className="font-mono">acl.yaml</span> at ingestion.
            </Perm>
            <Perm title="Permission-aware retrieval" on={k.access === "groups"}>
              {k.access === "groups"
                ? "The filter is applied before ranking, so a document you can't read never reaches the model."
                : "Not applied: every user of the agent can retrieve every document in this index."}
            </Perm>
            <Perm title="Fail-closed access" on={k.access === "groups"}>
              {k.access === "groups"
                ? "Unknown caller or directory failure means no documents, never everything. The agent says it couldn't search."
                : "Not applicable to a public index."}
            </Perm>
          </ul>
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Keeping it current" description="Ingestion runs in the deploy pipeline, or by hand." />
        <CardBody>
          <div className="flex items-center gap-1 rounded-md border border-zinc-200 bg-zinc-50 py-1 pl-3 pr-1 font-mono text-xs text-zinc-700">
            <Terminal aria-hidden className="size-3.5 shrink-0 text-zinc-500" />
            <span className="min-w-0 flex-1 truncate">{ingest}</span>
            <CopyButton value={ingest} label="Copy command" />
          </div>
          <p className="mt-2 text-xs text-zinc-500">Incremental: unchanged files are skipped, removed files are deleted from the index.</p>
        </CardBody>
      </Card>
    </div>
  );
}

function SourceRow({ name, detail, children }: { name: string; detail: ReactNode; children: ReactNode }) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-x-6 gap-y-1 px-5 py-3">
      <div className="min-w-0">
        <div className="text-sm text-zinc-900">{name}</div>
        <div className="text-[13px] text-zinc-500">{detail}</div>
      </div>
      {children}
    </li>
  );
}

function Perm({ title, on, children }: { title: string; on: boolean; children: ReactNode }) {
  return (
    <li className="rounded-md border border-zinc-200 p-4">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-zinc-900">{title}</span>
        <StatusBadge tone={on ? "ok" : "neutral"}>{on ? "On" : "Off"}</StatusBadge>
      </div>
      <p className="mt-1.5 text-[13px] leading-relaxed text-zinc-500">{children}</p>
    </li>
  );
}

// ------------------------------------------------------------------ telemetry
export function TelemetryPage() {
  return (
    <Page>
      <PageHeader title="Telemetry" description="Monitor agent executions, tools, latency and errors." />
      <WithOverview title="Telemetry">{(data) => <TelemetryBody data={data} />}</WithOverview>
    </Page>
  );
}

const METRICS = [
  { name: "Requests", metric: "agentkit.agent.runs", detail: "By outcome: ok, blocked, error" },
  { name: "Latency", metric: "agentkit.agent.run.duration", detail: "Per run, seconds" },
  { name: "Errors", metric: "agentkit.agent.runs{outcome=error}", detail: "Alert above 5% in 15 minutes" },
  { name: "Tool calls", metric: "execute_tool spans", detail: "Name, duration, status on every call" },
  { name: "Tokens", metric: "Total Tokens (gateway)", detail: "By team, agent and caller" },
];

function TelemetryBody({ data }: { data: Overview }) {
  const t = data.telemetry;
  const workbook = safeUrl(t.workbook_url);
  if (!t.exporter)
    return (
      <Card>
        <EmptyState icon={<Activity aria-hidden />} title="Telemetry not connected">
          Connect OpenTelemetry to view production traces and metrics. Set <span className="font-mono">APPLICATIONINSIGHTS_CONNECTION_STRING</span> (the
          Azure deployment does) or <span className="font-mono">AGENTKIT_OTLP_ENDPOINT</span>.
        </EmptyState>
      </Card>
    );
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader
          title="Export"
          description="Traces and metrics leave the service as they happen; dashboards query the workspace, not this service."
          action={
            workbook ? (
              <ButtonLink size="sm" href={workbook} external>
                <ExternalLink aria-hidden /> Operations workbook
              </ButtonLink>
            ) : undefined
          }
        />
        <CardBody>
          <dl className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <KeyValue label="Exporter">{t.exporter === "app_insights" ? "Azure Monitor (Application Insights)" : "OTLP"}</KeyValue>
            <KeyValue label="Status">
              <span title="An exporter is configured. Delivery isn't checked from here; the workbook shows what arrives.">
                <StatusDot tone="ok">Configured</StatusDot>
              </span>
            </KeyValue>
            <KeyValue label="Prompt content">{t.capture_content ? "Recorded in traces" : "Not recorded"}</KeyValue>
          </dl>
          {!workbook && (
            <InlineNotice className="mt-4">
              Set <span className="font-mono">AGENTKIT_CONSOLE_WORKBOOK_URL</span> to link the platform's operations workbook here (the platform
              deployment outputs it).
            </InlineNotice>
          )}
        </CardBody>
      </Card>

      <SectionTitle>Metrics this agent emits</SectionTitle>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        {METRICS.map((m) => (
          <Card key={m.name} className="p-4">
            <div className="text-[13px] font-medium text-zinc-900">{m.name}</div>
            <div className="mt-2 break-words font-mono text-2xs text-zinc-500">{m.metric}</div>
            <div className="mt-2 text-xs text-zinc-500">{m.detail}</div>
          </Card>
        ))}
      </div>

      <SectionTitle>Recent traces</SectionTitle>
      <Card>
        <EmptyState compact icon={<Activity aria-hidden />} title="Traces are in Application Insights" action={workbook ? <ButtonLink size="sm" href={workbook} external><ExternalLink aria-hidden /> Open workbook</ButtonLink> : undefined}>
          The service exports traces but doesn't keep them, so this console can't list them without reading your Log Analytics workspace.
          Search by session ID in Transaction search; every span carries the pseudonymized user, session and team.
        </EmptyState>
      </Card>
    </div>
  );
}

// ------------------------------------------------------------------ security
export function SecurityPage() {
  return (
    <Page>
      <PageHeader title="Security" description="Controls in force for this agent, read from its middleware stack and settings at runtime." />
      <WithOverview title="Security">
        {(data) => {
          const s = postureSummary(data.security);
          return (
            <div className="grid gap-6 xl:grid-cols-[1fr_320px]">
              <Card>
                <CardHeader title="Security posture" icon={<Shield aria-hidden />} action={<StatusBadge tone={s.tone}>{s.label}</StatusBadge>} />
                <CardBody className="py-1">
                  <PostureList controls={data.security} />
                </CardBody>
              </Card>
              <div className="space-y-4">
                <Card className="p-5">
                  <div className="text-2xs font-medium uppercase tracking-[0.06em] text-zinc-500">Controls on</div>
                  <div className="mt-2 text-3xl font-semibold tabular-nums text-zinc-950">
                    {s.on}
                    <span className="text-zinc-500"> / {s.total}</span>
                  </div>
                  <ul className="mt-3 space-y-1 text-[13px] text-zinc-600">
                    <li>{s.partial} partial</li>
                    <li>{s.off} off</li>
                  </ul>
                </Card>
                <InlineNotice>
                  In <span className="font-mono">prod</span> the service refuses to start unless sign-in is required, Prompt Shields is on,
                  model access goes through the gateway with managed identity, and prompt content stays out of telemetry.
                </InlineNotice>
              </div>
            </div>
          );
        }}
      </WithOverview>
    </Page>
  );
}

// ------------------------------------------------------------------ deployments
export function DeploymentsPage() {
  const { health } = useHealth();
  const evals = useLoad(api.evals);
  return (
    <Page>
      <PageHeader title="Deployments" description="What's running in this environment, and how it got here." />
      <WithOverview title="Deployments">
        {(data) => {
          const s = data.service;
          const run = safeUrl(s.run_url);
          const report = evals.data?.report;
          return (
            <div className="space-y-6">
              <Card>
                <CardHeader title={s.title} description={`${data.agent.name} · team ${s.team}`} icon={<Rocket aria-hidden />} />
                <Table label="Environments">
                  <thead>
                    <tr>
                      <Th>Environment</Th>
                      <Th>Health</Th>
                      <Th>Version</Th>
                      <Th className="hidden md:table-cell">Commit</Th>
                      <Th className="hidden md:table-cell">Deployed</Th>
                      <Th className="hidden lg:table-cell">Quality gate</Th>
                    </tr>
                  </thead>
                  <tbody>
                    <Tr>
                      <Td>
                        <div className="font-medium text-zinc-900">{environmentLabel(s.environment)}</div>
                        <div className="text-xs text-zinc-500">This console</div>
                      </Td>
                      <Td>
                        <HealthDot health={health} />
                      </Td>
                      <Td className="font-mono text-[13px]">{s.version}</Td>
                      <Td className="hidden md:table-cell">
                        {s.commit ? (
                          <span className="inline-flex items-center gap-1 font-mono text-[13px]">
                            <GitCommitHorizontal aria-hidden className="size-3.5 text-zinc-500" />
                            {s.commit.slice(0, 7)}
                          </span>
                        ) : (
                          <span className="text-zinc-500">—</span>
                        )}
                      </Td>
                      <Td className="hidden whitespace-nowrap md:table-cell">
                        {s.deployed_at ? dateTime(Date.parse(s.deployed_at) / 1000) : <span className="text-zinc-500">—</span>}
                      </Td>
                      <Td className="hidden lg:table-cell">
                        {report ? (
                          <StatusBadge tone={report.passed ? "ok" : "bad"}>{report.passed ? "Passed" : "Failed"}</StatusBadge>
                        ) : (
                          <span className="text-[13px] text-zinc-500">No report</span>
                        )}
                      </Td>
                    </Tr>
                  </tbody>
                </Table>
                <div className="border-t border-zinc-100 px-5 py-3 text-[13px] text-zinc-500">
                  Other environments run their own copy of this service; open their consoles to see them. Promotions go through the{" "}
                  <span className="font-mono">agent-deploy</span> workflow.
                </div>
              </Card>

              <div className="grid gap-6 lg:grid-cols-2">
                <Card>
                  <CardHeader title="Runtime" />
                  <CardBody>
                    <dl className="grid grid-cols-2 gap-x-6 gap-y-4">
                      <KeyValue label="Platform">{s.hosting.platform}</KeyValue>
                      <KeyValue label="App" mono>
                        {s.hosting.app ?? "—"}
                      </KeyValue>
                      <KeyValue label="Revision" mono>
                        {s.hosting.revision ?? "—"}
                      </KeyValue>
                      <KeyValue label="Replica" mono>
                        {s.hosting.replica ?? "—"}
                      </KeyValue>
                      <KeyValue label="Process started">{timeAgo(s.started_at)}</KeyValue>
                      <KeyValue label="agentkit" mono>
                        {s.kit_version ?? "—"}
                      </KeyValue>
                    </dl>
                  </CardBody>
                </Card>
                <Card>
                  <CardHeader title="Pipeline" />
                  <CardBody>
                    {run || s.commit ? (
                      <dl className="grid grid-cols-1 gap-4">
                        <KeyValue label="Commit" mono>
                          {s.commit ?? "—"}
                        </KeyValue>
                        <KeyValue label="Deploy run">
                          {run ? (
                            <a href={run} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-accent-700 hover:underline">
                              Open run page <ExternalLink aria-hidden className="size-3.5" />
                            </a>
                          ) : (
                            "—"
                          )}
                        </KeyValue>
                      </dl>
                    ) : (
                      <EmptyState compact title="No pipeline metadata">
                        Deployments by <span className="font-mono">agent-deploy</span> set the commit, time and run link. This instance was started
                        another way.
                      </EmptyState>
                    )}
                  </CardBody>
                </Card>
              </div>
            </div>
          );
        }}
      </WithOverview>
    </Page>
  );
}

// ------------------------------------------------------------------ settings
export function SettingsPage() {
  return (
    <Page>
      <PageHeader title="Settings" description="The runtime configuration of this service. Read-only: settings come from AGENTKIT_* environment variables." />
      <WithOverview title="Settings">
        {(data) => (
          <div className="space-y-6">
            <Card>
              <CardHeader title="Service" />
              <CardBody>
                <dl className="grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-4">
                  <KeyValue label="Name" mono>
                    {data.service.name}
                  </KeyValue>
                  <KeyValue label="Environment">{environmentLabel(data.service.environment)}</KeyValue>
                  <KeyValue label="Team">{data.service.team}</KeyValue>
                  <KeyValue label="Version" mono>
                    {data.service.version}
                  </KeyValue>
                </dl>
              </CardBody>
            </Card>
            <Card>
              <CardHeader title="Model and limits" />
              <CardBody>
                <dl className="grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-4">
                  <KeyValue label="Model" mono>
                    {data.agent.model}
                  </KeyValue>
                  <KeyValue label="Gateway">{data.agent.gateway ? "Yes" : "No"}</KeyValue>
                  <KeyValue label="Auth mode" mono>
                    {data.agent.auth_mode}
                  </KeyValue>
                  <KeyValue label="Model calls per run">{data.agent.limits.max_iterations}</KeyValue>
                  <KeyValue label="Tool calls per run">{data.agent.limits.max_function_calls}</KeyValue>
                  <KeyValue label="Run timeout">{duration(data.agent.limits.max_run_seconds)}</KeyValue>
                  <KeyValue label="Session tokens">{number(data.agent.limits.session_token_budget)}</KeyValue>
                  <KeyValue label="Session lifetime">{duration(data.agent.limits.session_ttl_seconds)}</KeyValue>
                </dl>
              </CardBody>
            </Card>
            <Card>
              <CardHeader title="Sessions and approvals" />
              <CardBody>
                <dl className="grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-4">
                  <KeyValue label="Session store" mono>
                    {data.sessions.store}
                  </KeyValue>
                  <KeyValue label="Shared across replicas">{data.sessions.shared ? "Yes" : "No"}</KeyValue>
                  <KeyValue label="Approval mode">{{ confirmation: "Requester confirms", approver: "Approver role", separation: "Separation of duties" }[data.approvals.mode]}</KeyValue>
                  <KeyValue label="Approver role" mono>
                    {data.approvals.approver_role ?? "—"}
                  </KeyValue>
                </dl>
              </CardBody>
            </Card>
            <p className="text-xs text-zinc-500">
              Change these in the service's Bicep or <span className="font-mono">.env</span> and redeploy. See{" "}
              {data.links.docs ? (
                <a href={`${data.links.docs}/configuration.md`} target="_blank" rel="noopener noreferrer" className="text-accent-700 underline underline-offset-2">
                  configuration
                </a>
              ) : (
                "docs/configuration.md"
              )}
              .
            </p>
            <div className="flex flex-wrap gap-2">
              {data.channels.map((c) => (
                <Tag key={c.id} mono>
                  {c.path ?? c.id}
                </Tag>
              ))}
            </div>
          </div>
        )}
      </WithOverview>
    </Page>
  );
}
