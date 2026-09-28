import { BookOpen, ChevronDown, ExternalLink, Search, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/overlay";
import { InlineNotice, Skeleton } from "@/components/ui/states";
import { Tag } from "@/components/ui/status";
import { templatesApi, type AgentTemplate, type TemplateDetail, type TemplateLibrary } from "@/lib/api";
import { useLoad } from "@/lib/data";
import { cn } from "@/lib/format";

function matches(t: AgentTemplate, q: string): boolean {
  if (!q) return true;
  const hay = [t.name, t.vibe, ...t.services].join(" ").toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w));
}

/** Browse template libraries and pick one: a persona and procedure the new agent starts from. */
export function TemplatePicker({
  current,
  onPick,
  onClose,
}: {
  current: string | null;
  onPick: (template: AgentTemplate, library: TemplateLibrary) => void;
  onClose: () => void;
}) {
  const catalog = useLoad(templatesApi.list);
  const [query, setQuery] = useState("");
  const [focus, setFocus] = useState<string | null>(current);
  const libraries = catalog.data?.libraries ?? [];
  const all = useMemo(() => libraries.flatMap((l) => l.groups.flatMap((g) => g.templates)), [libraries]);
  const focused = all.find((t) => t.id === focus) ?? null;
  const library = libraries.find((l) => l.name === focused?.library) ?? null;
  const count = all.filter((t) => matches(t, query)).length;

  return (
    <Dialog
      open
      onOpenChange={(open) => !open && onClose()}
      className="max-w-5xl"
      title="Start from a template"
      description="A specialist's role and working procedure become the new agent's instructions. You can edit them afterwards."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!focused || !library} onClick={() => focused && library && onPick(focused, library)}>
            Use this template
          </Button>
        </>
      }
    >
      {catalog.error ? (
        <InlineNotice tone="bad">{catalog.error.message}</InlineNotice>
      ) : !catalog.data ? (
        <div className="space-y-2"><Skeleton className="h-4 w-1/2" /><Skeleton className="h-4 w-1/3" /></div>
      ) : libraries.length === 0 ? (
        <p className="text-[13px] text-zinc-600">
          No template libraries on this server. Add one with <span className="font-mono">python3 agentctl.py templates add &lt;name&gt; &lt;git-url&gt;</span>.
        </p>
      ) : (
        <div className="grid h-[min(65vh,640px)] grid-rows-[minmax(0,1fr)_minmax(0,1fr)] gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)] md:grid-rows-[minmax(0,1fr)]">
          <div className="flex min-h-0 flex-col">
            <label className="relative block">
              <span className="sr-only">Search templates</span>
              <Search aria-hidden className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-zinc-400" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search: contracts, arbitration, HIPAA…"
                className="h-9 w-full rounded-md border border-zinc-200 pl-8 pr-3 text-sm outline-none focus:border-zinc-400 focus:ring-2 focus:ring-accent-500/20"
              />
            </label>
            <p className="mt-1.5 text-xs text-zinc-500" aria-live="polite">{count} of {all.length} templates</p>
            <div className="mt-2 min-h-0 flex-1 space-y-4 overflow-y-auto pr-1">
              {libraries.map((lib) => (
                <section key={lib.name} aria-label={lib.title}>
                  <h3 className="text-[13px] font-semibold text-zinc-900">
                    {lib.title}
                    {lib.publisher && <span className="font-normal text-zinc-500"> · {lib.publisher}</span>}
                    {lib.license && <span className="ml-2 align-middle"><Tag>{lib.license}</Tag></span>}
                  </h3>
                  {lib.groups.map((g) => {
                    const shown = g.templates.filter((t) => matches(t, query));
                    if (!shown.length) return null;
                    return (
                      <div key={g.id} className="mt-2">
                        <h4 className="text-2xs font-semibold uppercase tracking-wide text-zinc-500">{g.title}</h4>
                        <ul className="mt-1 space-y-1" aria-label={g.title}>
                          {shown.map((t) => (
                            <li key={t.id}>
                              <button
                                type="button"
                                aria-pressed={t.id === focus}
                                onClick={() => setFocus(t.id)}
                                onDoubleClick={() => onPick(t, lib)}
                                className={cn(
                                  "flex w-full gap-2.5 rounded-md border px-2.5 py-2 text-left",
                                  t.id === focus ? "border-zinc-900 bg-zinc-50" : "border-zinc-200 hover:border-zinc-300",
                                )}
                              >
                                <span aria-hidden className="w-5 shrink-0 text-base leading-5">{t.emoji}</span>
                                <span className="min-w-0">
                                  <span className="block text-sm text-zinc-900">{t.name}</span>
                                  <span className="block truncate text-xs text-zinc-500">{t.vibe}</span>
                                </span>
                              </button>
                            </li>
                          ))}
                        </ul>
                      </div>
                    );
                  })}
                </section>
              ))}
            </div>
          </div>
          <div className="min-h-0 overflow-y-auto rounded-lg border border-zinc-200 p-4">
            {focused && library ? <TemplateDetails template={focused} library={library} /> : (
              <p className="text-[13px] text-zinc-500">Pick a template to see what it adds.</p>
            )}
          </div>
        </div>
      )}
    </Dialog>
  );
}

function TemplateDetails({ template, library }: { template: AgentTemplate; library: TemplateLibrary }) {
  const [detail, setDetail] = useState<TemplateDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    let live = true;
    setDetail(null);
    setError(null);
    setOpen(false);
    templatesApi.get(template.id).then((d) => live && setDetail(d), (e: unknown) => live && setError(e instanceof Error ? e.message : "Couldn't load it"));
    return () => { live = false; };
  }, [template.id]);
  const rules = library.rules.split("\n").map((l) => l.replace(/^-\s*/, "").trim()).filter(Boolean);
  return (
    <div className="space-y-4 text-[13px]">
      <div>
        <h3 className="text-[15px] font-semibold text-zinc-950"><span aria-hidden>{template.emoji} </span>{template.name}</h3>
        <p className="mt-1 text-zinc-600">{template.vibe}</p>
        {library.source && (
          <a href={library.source} target="_blank" rel="noreferrer noopener" className="mt-1 inline-flex items-center gap-1 text-xs text-accent-700 hover:underline">
            {library.source.replace(/^https:\/\//, "")}{library.commit && ` @ ${library.commit.slice(0, 7)}`} <ExternalLink aria-hidden className="size-3" />
          </a>
        )}
      </div>
      {template.services.length > 0 && (
        <div>
          <h4 className="font-medium text-zinc-900">Its scope (goes into the charter)</h4>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-zinc-600">{template.services.map((s) => <li key={s}>{s}</li>)}</ul>
        </div>
      )}
      {rules.length > 0 && (
        <div>
          <h4 className="flex items-center gap-1.5 font-medium text-zinc-900"><ShieldCheck aria-hidden className="size-4 text-emerald-600" /> Rules added on top</h4>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-zinc-600">{rules.map((r) => <li key={r}>{r}</li>)}</ul>
          <p className="mt-1 text-xs text-zinc-500">They take precedence over the role, together with the kit's own rules (tool use, boundaries, guardrails).</p>
        </div>
      )}
      {library.evals.length > 0 && (
        <div>
          <h4 className="font-medium text-zinc-900">Eval cases added to its quality gate</h4>
          <p className="mt-1 flex flex-wrap gap-1.5">{library.evals.map((e) => <Tag key={e}>{e}</Tag>)}</p>
        </div>
      )}
      <div>
        <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
          className="inline-flex items-center gap-1.5 font-medium text-zinc-900 hover:text-accent-700">
          <BookOpen aria-hidden className="size-4" /> {open ? "Hide" : "Read"} the instructions
          <ChevronDown aria-hidden className={cn("size-4 transition-transform", open && "rotate-180")} />
        </button>
        {open && (error ? <InlineNotice tone="bad" className="mt-2">{error}</InlineNotice> : !detail ? <Skeleton className="mt-2 h-4 w-1/2" /> : (
          <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded-md bg-zinc-50 p-3 font-mono text-[11.5px] leading-relaxed text-zinc-700">{detail.body}</pre>
        ))}
      </div>
    </div>
  );
}
