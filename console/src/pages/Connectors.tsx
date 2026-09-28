// Connectors: the platform's catalog of MCP servers (a CRM, an issue tracker, a database) that agents on
// this server may use. Admins add them and choose the allowed tools; the credential stays in the platform.

import { ArrowLeft, CircleCheck, ExternalLink, KeyRound, Loader2, Plug, Plus, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, Eyebrow } from "@/components/ui/card";
import { Page, PageHeader } from "@/components/ui/page";
import { EmptyState, ErrorState, InlineNotice, LoadingRegion, Skeleton } from "@/components/ui/states";
import { StatusBadge, Tag } from "@/components/ui/status";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import {
  CONNECTOR_NAME,
  CONNECTOR_PRESETS,
  OAUTH_ONLY,
  connectorsApi,
  type Connector,
  type ConnectorPreset,
  type ConnectorTool,
} from "@/lib/api";
import { useLoad } from "@/lib/data";
import { cn } from "@/lib/format";
import { Link, useRouter } from "@/lib/router";

const input =
  "h-9 w-full rounded-md border border-zinc-200 px-3 text-sm outline-none focus:border-zinc-400 focus:ring-2 focus:ring-accent-500/20";

function authLabel(c: Pick<Connector, "auth" | "header">): string {
  return c.auth === "bearer" ? "Bearer token" : c.auth === "header" ? `API key (${c.header})` : "No sign-in";
}

function Crumb({ here }: { here: string }) {
  return (
    <nav aria-label="Breadcrumb" className="text-[13px] text-zinc-500">
      <Link to="/connectors" className="hover:text-zinc-900">
        Connectors
      </Link>
      <span aria-hidden className="px-1.5 text-zinc-300">/</span>
      <span aria-current="page">{here}</span>
    </nav>
  );
}

// ------------------------------------------------------------------ catalog
export function ConnectorsPage() {
  const { data, error, reload } = useLoad(connectorsApi.list);
  const { navigate } = useRouter();
  return (
    <Page wide>
      <PageHeader
        title="Connectors"
        description="MCP servers the agents on this server may use. The credential stays in the platform; agents only get the tools you allow, and anything that changes data waits for the person in the chat to approve."
        actions={
          data?.can_manage && (
            <Button size="sm" variant="primary" onClick={() => navigate("/connectors/new")}>
              <Plus aria-hidden /> Add connector
            </Button>
          )
        }
      />
      {data && !data.vault && (
        <InlineNotice tone="warn" className="mb-4">
          The platform has no encryption key yet, so credentials can't be stored. On the server: python3 agentctl.py platform --admins … then docker compose up -d platform.
        </InlineNotice>
      )}
      <Card>
        {error && !data ? (
          <ErrorState title="Unable to load connectors" message={error.message} onRetry={reload} />
        ) : !data ? (
          <LoadingRegion label="Loading connectors" className="space-y-3 p-5">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-4 w-1/2" />
          </LoadingRegion>
        ) : data.connectors.length === 0 ? (
          <EmptyState
            icon={<Plug aria-hidden />}
            title="No connectors yet"
            action={data.can_manage ? <Button size="sm" variant="primary" onClick={() => navigate("/connectors/new")}><Plus aria-hidden /> Add connector</Button> : undefined}
          >
            Connect Linear, GitHub, Stripe, Supabase or any MCP server, then choose which agents may use it.
          </EmptyState>
        ) : (
          <Table label="Connectors">
            <thead>
              <tr>
                <Th>Connector</Th>
                <Th className="hidden md:table-cell">Sign-in</Th>
                <Th>Tools allowed</Th>
                <Th className="hidden lg:table-cell">Need approval</Th>
                <Th>Used by</Th>
              </tr>
            </thead>
            <tbody>
              {data.connectors.map((c) => (
                <Tr key={c.name} interactive onClick={() => navigate(`/connectors/${encodeURIComponent(c.name)}`)}>
                  <Td>
                    <Link to={`/connectors/${encodeURIComponent(c.name)}`} className="font-medium text-zinc-950 hover:underline">
                      {c.title}
                    </Link>
                    <div className="font-mono text-xs text-zinc-500">{c.host}</div>
                  </Td>
                  <Td className="hidden text-[13px] md:table-cell">{authLabel(c)}</Td>
                  <Td className="text-[13px]">
                    {c.allowed.length} of {c.tools.length || c.allowed.length}
                  </Td>
                  <Td className="hidden text-[13px] lg:table-cell">{c.approval.length ? `${c.approval.length} write tools` : "None"}</Td>
                  <Td className="text-[13px]">{c.used_by.length ? c.used_by.map((a) => (a === "sample" ? "Order Status (sample)" : a)).join(", ") : "No agents yet"}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <InlineNotice className="mt-4">
        Coming next: services whose hosted MCP servers need each person to sign in ({OAUTH_ONLY.join(", ")}). Give an agent a connector on
        the Agents page, or when you create it.
      </InlineNotice>
    </Page>
  );
}

// ------------------------------------------------------------------ add
export function NewConnectorPage() {
  const { navigate } = useRouter();
  const [preset, setPreset] = useState<ConnectorPreset | "custom" | null>(null);
  const [name, setName] = useState("");
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [auth, setAuth] = useState<Connector["auth"]>("bearer");
  const [header, setHeader] = useState("X-API-Key");
  const [secret, setSecret] = useState("");
  const [tools, setTools] = useState<ConnectorTool[] | null>(null);
  const [allowed, setAllowed] = useState<Set<string>>(new Set());
  const [approval, setApproval] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<"test" | "save" | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const choose = (p: ConnectorPreset | "custom") => {
    setPreset(p);
    setTools(null);
    setProblem(null);
    if (p === "custom") {
      setName("");
      setTitle("");
      setUrl("https://");
      setAuth("bearer");
    } else {
      setName(p.id);
      setTitle(p.title);
      setUrl(p.url);
      setAuth(p.auth);
      if (p.header) setHeader(p.header);
    }
  };

  const test = async () => {
    setBusy("test");
    setProblem(null);
    try {
      const found = (await connectorsApi.test({ url, auth, header: auth === "header" ? header : undefined, secret })).tools;
      setTools(found);
      // a safe start: read-only tools allowed; anything else allowed only after an approval, and off by default
      setAllowed(new Set(found.filter((t) => t.read_only).map((t) => t.name)));
      setApproval(new Set(found.filter((t) => !t.read_only).map((t) => t.name)));
    } catch (e) {
      setTools(null);
      setProblem(e instanceof Error ? e.message : "The server couldn't be reached.");
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    if (!tools) return;
    setBusy("save");
    setProblem(null);
    try {
      await connectorsApi.add({
        name, title: title.trim() || name, url, auth, header: auth === "header" ? header : undefined, secret: auth === "none" ? undefined : secret,
        preset: preset && preset !== "custom" ? preset.id : undefined, tools, allowed: [...allowed],
        approval: [...approval].filter((t) => allowed.has(t)),
      });
      navigate(`/connectors/${encodeURIComponent(name)}`);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "The connector couldn't be saved.");
    } finally {
      setBusy(null);
    }
  };

  const nameOk = CONNECTOR_NAME.test(name);
  const canTest = /^https?:\/\/./.test(url) && (auth === "none" || secret.length > 0) && (auth !== "header" || header.length > 0);

  return (
    <Page>
      <PageHeader eyebrow={<Crumb here="Add" />} title="Add a connector" description="Connect an MCP server. Nothing is saved until you choose its tools." />
      {!preset ? (
        <>
          <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" aria-label="Services">
            {CONNECTOR_PRESETS.map((p) => (
              <li key={p.id}>
                <button
                  type="button"
                  onClick={() => choose(p)}
                  className="h-full w-full rounded-lg border border-zinc-200 bg-white p-4 text-left transition-colors hover:border-zinc-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
                >
                  <div className="flex items-center gap-2 text-sm font-medium text-zinc-950">
                    <Plug aria-hidden className="size-4 text-accent-600" /> {p.title}
                  </div>
                  <p className="mt-1 text-[13px] text-zinc-500">{p.help}</p>
                </button>
              </li>
            ))}
            <li>
              <button
                type="button"
                onClick={() => choose("custom")}
                className="h-full w-full rounded-lg border border-dashed border-zinc-300 bg-white p-4 text-left hover:border-zinc-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
              >
                <div className="flex items-center gap-2 text-sm font-medium text-zinc-950">
                  <Plus aria-hidden className="size-4" /> Any MCP server
                </div>
                <p className="mt-1 text-[13px] text-zinc-500">Your own or a vendor's, over HTTPS, with no sign-in, a bearer token or an API-key header.</p>
              </button>
            </li>
          </ul>
          <InlineNotice className="mt-4">Not available yet (each person signs in with OAuth): {OAUTH_ONLY.join(", ")}.</InlineNotice>
        </>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[1fr_300px]">
          <Card>
            <CardBody className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <Field id="c-title" label="Name shown to people">
                  <input id="c-title" className={input} value={title} maxLength={60} onChange={(e) => setTitle(e.target.value)} />
                </Field>
                <Field id="c-name" label="Short name" hint={<>Its tools appear to agents as <span className="font-mono">{name || "name"}_…</span></>}>
                  <input id="c-name" className={input} value={name} maxLength={31} aria-invalid={name.length > 0 && !nameOk}
                    onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ""))} />
                </Field>
              </div>
              <Field id="c-url" label="MCP server URL" hint="HTTPS, a public service.">
                <input id="c-url" className={cn(input, "font-mono text-[13px]")} value={url} onChange={(e) => { setUrl(e.target.value); setTools(null); }} />
              </Field>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field id="c-auth" label="Sign-in">
                  <select id="c-auth" className={input} value={auth} onChange={(e) => { setAuth(e.target.value as Connector["auth"]); setTools(null); }}>
                    <option value="bearer">Bearer token</option>
                    <option value="header">API-key header</option>
                    <option value="none">None</option>
                  </select>
                </Field>
                {auth === "header" && (
                  <Field id="c-header" label="Header name">
                    <input id="c-header" className={input} value={header} onChange={(e) => setHeader(e.target.value.replace(/[^A-Za-z0-9-]/g, ""))} />
                  </Field>
                )}
              </div>
              {auth !== "none" && (
                <Field id="c-secret" label={preset !== "custom" ? preset.secretLabel : "Token or API key"} hint="Stored encrypted on the server. Nobody can read it back, including you.">
                  <input id="c-secret" type="password" autoComplete="off" className={input} value={secret} onChange={(e) => { setSecret(e.target.value); setTools(null); }} />
                </Field>
              )}
              {problem && <InlineNotice tone="bad">{problem}</InlineNotice>}
              {!tools && (
                <div className="flex justify-end gap-2">
                  <Button onClick={() => setPreset(null)}><ArrowLeft aria-hidden /> Back</Button>
                  <Button variant="primary" disabled={!canTest || busy !== null} onClick={() => void test()}>
                    {busy === "test" ? <Loader2 aria-hidden className="animate-spin" /> : <RefreshCw aria-hidden />} Connect and list tools
                  </Button>
                </div>
              )}
            </CardBody>
            {tools && (
              <>
                <ToolPicker tools={tools} allowed={allowed} approval={approval} onAllowed={setAllowed} onApproval={setApproval} />
                <div className="flex justify-end gap-2 border-t border-zinc-100 bg-zinc-50/50 px-5 py-3">
                  <Button onClick={() => setTools(null)}>Change the connection</Button>
                  <Button variant="primary" disabled={!nameOk || allowed.size === 0 || busy !== null} onClick={() => void save()}>
                    {busy === "save" ? <Loader2 aria-hidden className="animate-spin" /> : <CircleCheck aria-hidden />} Save connector
                  </Button>
                </div>
              </>
            )}
          </Card>
          <div className="space-y-4">
            {preset !== "custom" && (
              <Card className="p-5">
                <Eyebrow>{preset.title}</Eyebrow>
                <p className="mt-2 text-[13px] text-zinc-600">{preset.help}</p>
                <a href={preset.docs} target="_blank" rel="noopener noreferrer" className="mt-3 inline-flex items-center gap-1 text-[13px] font-medium text-accent-700 hover:underline">
                  Setup guide <ExternalLink aria-hidden className="size-3.5" />
                </a>
              </Card>
            )}
            <Card className="p-5">
              <Eyebrow>How agents use it</Eyebrow>
              <ul className="mt-3 space-y-2 text-[13px] text-zinc-600">
                <li className="flex gap-2"><KeyRound aria-hidden className="mt-0.5 size-4 shrink-0 text-accent-600" /> The credential never reaches an agent: the platform adds it to each call.</li>
                <li className="flex gap-2"><ShieldCheck aria-hidden className="mt-0.5 size-4 shrink-0 text-accent-600" /> Only the tools you allow are callable, even if an agent is manipulated.</li>
                <li className="flex gap-2"><CircleCheck aria-hidden className="mt-0.5 size-4 shrink-0 text-accent-600" /> Write tools pause for the person in the chat to approve, and every call is logged.</li>
              </ul>
            </Card>
          </div>
        </div>
      )}
    </Page>
  );
}

function ToolPicker({
  tools, allowed, approval, onAllowed, onApproval, readOnly,
}: {
  tools: ConnectorTool[];
  allowed: Set<string>;
  approval: Set<string>;
  onAllowed: (s: Set<string>) => void;
  onApproval: (s: Set<string>) => void;
  readOnly?: boolean;
}) {
  const toggle = (set: Set<string>, name: string, on: boolean) => {
    const next = new Set(set);
    if (on) next.add(name);
    else next.delete(name);
    return next;
  };
  if (!tools.length) return <CardBody className="text-[13px] text-zinc-500">This server offers no tools.</CardBody>;
  return (
    <div className="border-t border-zinc-100">
      <CardHeader title={`Tools (${tools.length})`} description="Allow only what the agents need. Read-only tools start allowed; tools that change data start off and, when allowed, need approval." />
      <Table label="Tools">
        <thead>
          <tr>
            <Th>Tool</Th>
            <Th className="w-24 text-center">Allowed</Th>
            <Th className="w-32 text-center">Needs approval</Th>
          </tr>
        </thead>
        <tbody>
          {tools.map((t) => (
            <Tr key={t.name}>
              <Td>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[13px] text-zinc-900">{t.name}</span>
                  <Tag>{t.read_only ? "Read" : "Write"}</Tag>
                </div>
                {t.description && <p className="mt-0.5 line-clamp-2 text-xs text-zinc-500">{t.description}</p>}
              </Td>
              <Td className="text-center">
                <input type="checkbox" className="size-4 accent-zinc-900" checked={allowed.has(t.name)} disabled={readOnly}
                  aria-label={`Allow ${t.name}`} onChange={(e) => onAllowed(toggle(allowed, t.name, e.target.checked))} />
              </Td>
              <Td className="text-center">
                <input type="checkbox" className="size-4 accent-zinc-900" checked={approval.has(t.name)} disabled={readOnly || !allowed.has(t.name)}
                  aria-label={`Require approval for ${t.name}`} onChange={(e) => onApproval(toggle(approval, t.name, e.target.checked))} />
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}

// ------------------------------------------------------------------ one connector
export function ConnectorDetailPage({ name }: { name: string }) {
  const list = useLoad(connectorsApi.list);
  const connector = useMemo(() => list.data?.connectors.find((c) => c.name === name) ?? null, [list.data, name]);
  const manage = Boolean(list.data?.can_manage);
  const activity = useLoad(() => (manage ? connectorsApi.activity(name) : Promise.resolve({ activity: [] })), [manage, name]);
  const { navigate } = useRouter();
  const [allowed, setAllowed] = useState<Set<string> | null>(null);
  const [approval, setApproval] = useState<Set<string> | null>(null);
  const [tools, setTools] = useState<ConnectorTool[] | null>(null);
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ tone: "info" | "bad"; text: string } | null>(null);

  if (list.error && !list.data) return <Page><ErrorState title="Unable to load the connector" message={list.error.message} onRetry={list.reload} /></Page>;
  if (!list.data) return <Page><LoadingRegion label="Loading" className="space-y-3"><Skeleton className="h-4 w-1/3" /></LoadingRegion></Page>;
  if (!connector)
    return (
      <Page>
        <Card><EmptyState title="Connector not found" action={<Link to="/connectors" className="text-sm text-accent-700">All connectors</Link>}>There's no connector named {name}.</EmptyState></Card>
      </Page>
    );

  const shownTools = tools ?? connector.tools;
  const allowedNow = allowed ?? new Set(connector.allowed);
  const approvalNow = approval ?? new Set(connector.approval);
  const dirty = allowed !== null || approval !== null || tools !== null;

  const run = async (label: string, action: () => Promise<string | void>) => {
    setBusy(label);
    setNote(null);
    try {
      const text = await action();
      if (text) setNote({ tone: "info", text });
      list.reload();
    } catch (e) {
      setNote({ tone: "bad", text: e instanceof Error ? e.message : "That didn't work." });
    } finally {
      setBusy(null);
    }
  };

  return (
    <Page wide>
      <PageHeader
        eyebrow={<Crumb here={connector.title} />}
        title={connector.title}
        description={<span className="font-mono text-[13px]">{connector.url}</span>}
        meta={
          <>
            <Tag>{authLabel(connector)}</Tag>
            {connector.created_by && <span className="text-[13px] text-zinc-500">Added by {connector.created_by}</span>}
          </>
        }
        actions={
          manage && (
            <Button size="sm" disabled={busy !== null || connector.used_by.length > 0}
              title={connector.used_by.length ? "Take it off the agents using it first" : undefined}
              onClick={() => window.confirm(`Remove ${connector.title}? Its stored credential is deleted.`) &&
                void run("remove", async () => { await connectorsApi.remove(connector.name); navigate("/connectors"); })}>
              <Trash2 aria-hidden /> Remove
            </Button>
          )
        }
      />
      {note && <InlineNotice tone={note.tone} className="mb-4">{note.text}</InlineNotice>}
      <div className="grid gap-6 lg:grid-cols-[1fr_340px]">
        <Card>
          <ToolPicker tools={shownTools} allowed={allowedNow} approval={approvalNow} readOnly={!manage}
            onAllowed={(s) => setAllowed(s)} onApproval={(s) => setApproval(s)} />
          {manage && (
            <div className="flex flex-wrap justify-end gap-2 border-t border-zinc-100 bg-zinc-50/50 px-5 py-3">
              <Button disabled={busy !== null} onClick={() => void run("refresh", async () => {
                setTools((await connectorsApi.test({ name: connector.name })).tools);
                return "Tools listed again from the server. Save to keep any changes.";
              })}>
                {busy === "refresh" ? <Loader2 aria-hidden className="animate-spin" /> : <RefreshCw aria-hidden />} List tools again
              </Button>
              <Button variant="primary" disabled={!dirty || allowedNow.size === 0 || busy !== null} onClick={() => void run("save", async () => {
                const res = await connectorsApi.update(connector.name, {
                  tools: shownTools, allowed: [...allowedNow], approval: [...approvalNow].filter((t) => allowedNow.has(t)),
                });
                setAllowed(null);
                setApproval(null);
                setTools(null);
                return res.job ? `Saved. Restarting ${res.used_by.join(", ")} so they pick up the change.` : "Saved.";
              })}>
                {busy === "save" ? <Loader2 aria-hidden className="animate-spin" /> : <CircleCheck aria-hidden />} Save tools
              </Button>
            </div>
          )}
        </Card>
        <div className="space-y-4">
          <Card>
            <CardHeader title="Used by" />
            <CardBody className="text-[13px] text-zinc-600">
              {connector.used_by.length ? (
                <ul className="space-y-1">{connector.used_by.map((a) => <li key={a} className="font-mono">{a === "sample" ? "Order Status Agent (sample)" : a}</li>)}</ul>
              ) : (
                "No agents yet. Give it to an agent on the Agents page."
              )}
              <div className="mt-3"><Link to="/agents" className="text-[13px] font-medium text-accent-700 hover:underline">Choose connectors for agents</Link></div>
            </CardBody>
          </Card>
          {manage && connector.auth !== "none" && (
            <Card>
              <CardHeader title="Credential" description="Stored encrypted. Replace it when it's rotated." />
              <CardBody className="space-y-2">
                <label htmlFor="rotate" className="sr-only">New credential</label>
                <input id="rotate" type="password" autoComplete="off" className={input} value={secret} placeholder="New token or key" onChange={(e) => setSecret(e.target.value)} />
                <Button size="sm" disabled={!secret || busy !== null} onClick={() => void run("secret", async () => {
                  await connectorsApi.update(connector.name, { secret });
                  setSecret("");
                  return "Credential replaced.";
                })}>Replace credential</Button>
              </CardBody>
            </Card>
          )}
          {manage && (
            <Card>
              <CardHeader title="Recent calls" description="Tool calls through the platform since it last started." />
              {activity.data?.activity.length ? (
                <ul className="divide-y divide-zinc-100 text-[13px]">
                  {activity.data.activity.slice(0, 20).map((a, i) => (
                    <li key={i} className="flex items-center justify-between gap-2 px-5 py-2">
                      <span className="min-w-0 truncate"><span className="font-mono">{a.tool}</span> <span className="text-zinc-500">by {a.agent}</span></span>
                      <StatusBadge tone={a.allowed ? "ok" : "bad"}>{a.allowed ? "Allowed" : "Refused"}</StatusBadge>
                    </li>
                  ))}
                </ul>
              ) : (
                <CardBody className="text-[13px] text-zinc-500">No calls yet.</CardBody>
              )}
            </Card>
          )}
        </div>
      </div>
    </Page>
  );
}

function Field({ id, label, hint, children }: { id: string; label: string; hint?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="text-[13px] font-medium text-zinc-900">{label}</label>
      <div className="mt-1.5">{children}</div>
      {hint && <p className="mt-1 text-xs text-zinc-500">{hint}</p>}
    </div>
  );
}
