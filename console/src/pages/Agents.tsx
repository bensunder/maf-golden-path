import { ExternalLink, MessageSquareText, Plus, Shield } from "lucide-react";

import { HealthDot, MoreLink, PostureList, RuntimeGrid, ToolList, useHealth } from "@/components/agent";
import { Button, ButtonLink } from "@/components/ui/button";
import { Card, CardBody, CardHeader, KeyValue } from "@/components/ui/card";
import { Page, PageHeader } from "@/components/ui/page";
import { EmptyState, ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, Tag } from "@/components/ui/status";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import type { Overview } from "@/lib/api";
import { useOverview } from "@/lib/data";
import { duration, environmentLabel, number } from "@/lib/format";
import { Link, useRouter } from "@/lib/router";

// ------------------------------------------------------------------ list
export function AgentsPage() {
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
