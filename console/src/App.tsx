import { Compass } from "lucide-react";

import { AppShell } from "@/components/shell/AppShell";
import { MoreLink } from "@/components/agent";
import { Card } from "@/components/ui/card";
import { Page } from "@/components/ui/page";
import { EmptyState } from "@/components/ui/states";
import { OverviewProvider, SessionsProvider } from "@/lib/data";
import { PlaygroundProvider } from "@/lib/playground";
import { RouterProvider, useRouter } from "@/lib/router";
import { AgentDetailPage, AgentsPage } from "@/pages/Agents";
import { ApprovalsPage } from "@/pages/Approvals";
import { EvaluationsPage } from "@/pages/Evaluations";
import { DeploymentsPage, KnowledgePage, SecurityPage, SettingsPage } from "@/pages/Operations";
import { TelemetryPage } from "@/pages/Telemetry";
import { OverviewPage } from "@/pages/Overview";
import { CreateAgentPage, PlatformPage } from "@/pages/Platform";
import { ConnectorDetailPage, ConnectorsPage, NewConnectorPage } from "@/pages/Connectors";
import { NetworkPage } from "@/pages/Network";
import { PlaygroundPage } from "@/pages/Playground";
import { SessionsPage } from "@/pages/Sessions";
import { useEffect } from "react";

const TITLES: Record<string, string> = {
  "/": "Overview",
  "/agents": "Agents",
  "/agents/new": "Create agent",
  "/playground": "Playground",
  "/evaluations": "Evaluations",
  "/knowledge": "Knowledge",
  "/approvals": "Approvals",
  "/sessions": "Sessions",
  "/telemetry": "Telemetry",
  "/security": "Security",
  "/deployments": "Deployments",
  "/platform": "Platform",
  "/connectors": "Connectors",
  "/network": "Agent network",
  "/connectors/new": "Add connector",
  "/settings": "Settings",
};

function Routes() {
  const { path } = useRouter();

  useEffect(() => {
    const page = TITLES[path] ?? (path.startsWith("/agents/") ? "Agent" : path.startsWith("/connectors/") ? "Connector" : "Not found");
    const base = document.title.split(" · ").slice(-2).join(" · ");
    document.title = `${page} · ${base}`;
  }, [path]);

  switch (path) {
    case "/":
      return <OverviewPage />;
    case "/agents":
      return <AgentsPage />;
    case "/agents/new":
      return <CreateAgentPage />;
    case "/playground":
      return <PlaygroundPage />;
    case "/evaluations":
      return <EvaluationsPage />;
    case "/knowledge":
      return <KnowledgePage />;
    case "/approvals":
      return <ApprovalsPage />;
    case "/sessions":
      return <SessionsPage />;
    case "/telemetry":
      return <TelemetryPage />;
    case "/security":
      return <SecurityPage />;
    case "/deployments":
      return <DeploymentsPage />;
    case "/platform":
      return <PlatformPage />;
    case "/settings":
      return <SettingsPage />;
    case "/connectors":
      return <ConnectorsPage />;
    case "/network":
      return <NetworkPage />;
    case "/connectors/new":
      return <NewConnectorPage />;
  }
  if (path.startsWith("/connectors/")) return <ConnectorDetailPage name={path.slice("/connectors/".length)} />;
  if (path.startsWith("/agents/")) {
    let name = path.slice("/agents/".length);
    try {
      name = decodeURIComponent(name);
    } catch {
      /* a malformed address: shown as "not found" */
    }
    return <AgentDetailPage name={name} />;
  }
  return (
    <Page>
      <Card>
        <EmptyState icon={<Compass aria-hidden />} title="Page not found" action={<MoreLink to="/">Back to the overview</MoreLink>}>
          There's nothing at this address in the console.
        </EmptyState>
      </Card>
    </Page>
  );
}

export function App() {
  return (
    <RouterProvider>
      <OverviewProvider>
        <SessionsProvider>
          <PlaygroundProvider>
            <AppShell>
              <Routes />
            </AppShell>
          </PlaygroundProvider>
        </SessionsProvider>
      </OverviewProvider>
    </RouterProvider>
  );
}
