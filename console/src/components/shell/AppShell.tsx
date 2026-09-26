import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Bell, BookOpen, ChevronDown, ExternalLink, LogOut, Menu, Plus, Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { MenuContent, MenuItem, MenuLabel, MenuRoot, MenuSeparator, MenuTrigger } from "@/components/ui/overlay";
import { useOverview, useSessions } from "@/lib/data";
import { cn, environmentLabel, humanize } from "@/lib/format";
import { Link, useRouter } from "@/lib/router";

import { DOCS_ICON, PRIMARY_NAV, SECONDARY_NAV, isActive, type NavItem } from "./nav";

export function Wordmark({ className }: { className?: string }) {
  return (
    <span className={cn("flex items-center gap-2.5", className)}>
      <svg viewBox="0 0 32 32" className="size-6 shrink-0" aria-hidden>
        <rect width="32" height="32" rx="7" fill="#18181b" />
        <path d="M9 22.5 16 9l7 13.5" fill="none" stroke="#fff" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M12.2 17.5h7.6" stroke="#3b6ef6" strokeWidth="2.6" strokeLinecap="round" />
      </svg>
      <span className="text-[12.5px] font-semibold uppercase tracking-[0.12em] text-zinc-900">MAF Golden Path</span>
    </span>
  );
}

function NavLink({ item, path, onNavigate, badge }: { item: NavItem; path: string; onNavigate?: () => void; badge?: number }) {
  const active = isActive(item, path);
  const Icon = item.icon;
  return (
    <Link
      to={item.to}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={cn(
        "group relative flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13.5px] transition-colors duration-150",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500",
        active ? "bg-white font-medium text-zinc-950 shadow-card ring-1 ring-zinc-200" : "text-zinc-600 hover:bg-zinc-200/50 hover:text-zinc-900",
      )}
    >
      {active && <span aria-hidden className="absolute -left-3 top-1.5 bottom-1.5 w-0.5 rounded-full bg-accent-600" />}
      <Icon aria-hidden className={cn("size-4 shrink-0", active ? "text-accent-600" : "text-zinc-500 group-hover:text-zinc-600")} />
      <span className="flex-1">{item.label}</span>
      {badge ? (
        <span className="rounded-full bg-amber-100 px-1.5 text-[11px] font-semibold text-amber-900" aria-label={`${badge} pending`}>
          {badge}
        </span>
      ) : null}
    </Link>
  );
}

function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  const { path } = useRouter();
  const { data } = useOverview();
  const { pendingCount } = useSessions();
  const docs = data?.links.docs;
  return (
    <div className="flex h-full flex-col">
      <div className="flex h-14 shrink-0 items-center px-5">
        <Link to="/" onClick={onNavigate} className="rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500">
          <Wordmark />
        </Link>
      </div>
      <nav aria-label="Main" className="flex-1 overflow-y-auto px-3 pb-4">
        <ul className="space-y-0.5 pl-0">
          {PRIMARY_NAV.map((item) => (
            <li key={item.to}>
              <NavLink item={item} path={path} onNavigate={onNavigate} badge={item.to === "/approvals" ? pendingCount : undefined} />
            </li>
          ))}
        </ul>
        <div className="my-4 border-t border-zinc-200" />
        <ul className="space-y-0.5">
          {SECONDARY_NAV.map((item) => (
            <li key={item.to}>
              <NavLink item={item} path={path} onNavigate={onNavigate} />
            </li>
          ))}
          {docs && (
            <li>
              <a
                href={docs}
                target="_blank"
                rel="noopener noreferrer"
                className="group flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13.5px] text-zinc-600 transition-colors hover:bg-zinc-200/50 hover:text-zinc-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
              >
                <DOCS_ICON aria-hidden className="size-4 text-zinc-500 group-hover:text-zinc-600" />
                <span className="flex-1">Documentation</span>
                <ExternalLink aria-hidden className="size-3.5 text-zinc-500" />
                <span className="sr-only">(opens in a new tab)</span>
              </a>
            </li>
          )}
        </ul>
      </nav>
      <SidebarFooter />
    </div>
  );
}

function SidebarFooter() {
  const { data } = useOverview();
  if (!data) return null;
  return (
    <div className="border-t border-zinc-200 px-5 py-3 text-2xs leading-relaxed text-zinc-500">
      <div className="truncate">
        agentkit <span className="font-mono">{data.service.kit_version ?? "—"}</span>
        {data.service.maf_version && (
          <>
            {" · "}MAF <span className="font-mono">{data.service.maf_version}</span>
          </>
        )}
      </div>
    </div>
  );
}

const ENV_DOT: Record<string, string> = { prod: "bg-rose-500", test: "bg-amber-500", dev: "bg-accent-500", local: "bg-zinc-400" };

function EnvironmentBadge() {
  const { data } = useOverview();
  if (!data) return null;
  const env = data.service.environment;
  return (
    <span
      className="hidden items-center gap-2 rounded-full border border-zinc-200 bg-white px-2.5 py-1 text-xs font-medium text-zinc-700 sm:inline-flex"
      title={`Environment: ${environmentLabel(env)}`}
    >
      <span aria-hidden className={cn("size-1.5 rounded-full", ENV_DOT[env] ?? "bg-zinc-400")} />
      <span className="sr-only">Environment: </span>
      {environmentLabel(env)}
    </span>
  );
}

function Notifications() {
  const { pendingCount, states } = useSessions();
  const { navigate } = useRouter();
  const pending = useMemo(
    () =>
      Object.entries(states).flatMap(([sid, s]) => (s.status === "ok" ? s.info.pending.map((p) => ({ sid, ...p })) : [])),
    [states],
  );
  return (
    <MenuRoot>
      <MenuTrigger asChild>
        <Button variant="ghost" size="icon" className="relative" aria-label={pendingCount ? `Notifications: ${pendingCount} pending approvals` : "Notifications"}>
          <Bell aria-hidden />
          {pendingCount > 0 && <span aria-hidden className="absolute right-1.5 top-1.5 size-2 rounded-full bg-amber-500 ring-2 ring-white" />}
        </Button>
      </MenuTrigger>
      <MenuContent className="w-80">
        <MenuLabel>Notifications</MenuLabel>
        {pending.length === 0 ? (
          <div className="px-2.5 pb-3 pt-1 text-[13px] text-zinc-500">Nothing needs your attention.</div>
        ) : (
          pending.slice(0, 6).map((p) => (
            <MenuItem key={p.id} onSelect={() => navigate("/approvals")}>
              <span className="size-1.5 shrink-0 rounded-full bg-amber-500" aria-hidden />
              <span className="min-w-0 flex-1 truncate">
                {humanize(p.tool)} is waiting for approval
              </span>
            </MenuItem>
          ))
        )}
      </MenuContent>
    </MenuRoot>
  );
}

function UserMenu() {
  const { data } = useOverview();
  const user = data?.caller.user;
  const hosted = data && data.service.hosting.platform !== "Local";
  const name = user ? user.split("@")[0] : "Anonymous";
  const initials = name.replace(/[^A-Za-z]/g, "").slice(0, 2).toUpperCase() || "?";
  return (
    <MenuRoot>
      <MenuTrigger asChild>
        <button
          type="button"
          className="flex items-center gap-2 rounded-md py-1 pl-1 pr-1.5 text-sm text-zinc-700 transition-colors hover:bg-zinc-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
          aria-label={`Account: ${user ?? "not signed in"}`}
        >
          <span aria-hidden className="flex size-7 items-center justify-center rounded-full bg-zinc-900 text-[11px] font-semibold text-white">
            {initials}
          </span>
          <span className="hidden max-w-[140px] truncate font-medium md:inline">{name}</span>
          <ChevronDown aria-hidden className="size-3.5 text-zinc-500" />
        </button>
      </MenuTrigger>
      <MenuContent>
        <div className="px-2.5 py-2">
          <div className="truncate text-[13px] font-medium text-zinc-900">{user ?? "Not signed in"}</div>
          {user && (
            <div className="mt-0.5 text-xs text-zinc-500">
              {data?.caller.is_approver ? `Approver (${data.approvals.approver_role})` : "Signed in"}
            </div>
          )}
        </div>
        <MenuSeparator />
        {data?.links.docs && (
          <MenuItem asChild>
            <a href={data.links.docs} target="_blank" rel="noopener noreferrer">
              <BookOpen aria-hidden /> Documentation
            </a>
          </MenuItem>
        )}
        {hosted && (
          <MenuItem asChild>
            <a href="/.auth/logout">
              <LogOut aria-hidden /> Sign out
            </a>
          </MenuItem>
        )}
      </MenuContent>
    </MenuRoot>
  );
}

// ------------------------------------------------------------------ command menu (Ctrl/⌘ K)
function CommandMenu({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { navigate } = useRouter();
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const items = useMemo(() => {
    const all = [...PRIMARY_NAV, ...SECONDARY_NAV, { to: "/agents/new", label: "Create agent", icon: Plus }];
    const q = query.trim().toLowerCase();
    return q ? all.filter((i) => i.label.toLowerCase().includes(q)) : all;
  }, [query]);
  useEffect(() => {
    if (open) {
      setQuery("");
      setIndex(0);
    }
  }, [open]);
  const go = (to: string) => {
    onOpenChange(false);
    navigate(to);
  };
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-zinc-950/30 animate-fade-in motion-reduce:animate-none" />
        <DialogPrimitive.Content className="fixed left-1/2 top-[15vh] z-50 w-[calc(100vw-2rem)] max-w-md -translate-x-1/2 overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-pop animate-scale-in focus:outline-none motion-reduce:animate-none">
          <DialogPrimitive.Title className="sr-only">Go to</DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">Type to filter pages, Enter to open.</DialogPrimitive.Description>
          <div className="flex items-center gap-2 border-b border-zinc-100 px-4">
            <Search aria-hidden className="size-4 text-zinc-500" />
            <input
              autoFocus
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setIndex(0);
              }}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setIndex((i) => Math.min(i + 1, items.length - 1));
                } else if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setIndex((i) => Math.max(i - 1, 0));
                } else if (e.key === "Enter" && items[index]) {
                  go(items[index].to);
                }
              }}
              placeholder="Go to…"
              aria-label="Go to page"
              aria-controls="command-list"
              aria-activedescendant={items[index] ? `cmd-${items[index].to}` : undefined}
              className="h-12 flex-1 bg-transparent text-sm text-zinc-900 outline-none placeholder:text-zinc-500"
            />
            <kbd className="rounded border border-zinc-200 px-1.5 text-2xs text-zinc-500">Esc</kbd>
          </div>
          <ul id="command-list" role="listbox" aria-label="Pages" className="max-h-80 overflow-y-auto p-1.5">
            {items.length === 0 && <li className="px-3 py-6 text-center text-[13px] text-zinc-500">No matching page.</li>}
            {items.map((item, i) => {
              const Icon = item.icon;
              return (
                <li
                  key={item.to}
                  id={`cmd-${item.to}`}
                  role="option"
                  aria-selected={i === index}
                  onMouseEnter={() => setIndex(i)}
                  onClick={() => go(item.to)}
                  className={cn("flex cursor-pointer items-center gap-2.5 rounded-md px-3 py-2 text-[13.5px] text-zinc-700", i === index && "bg-zinc-100 text-zinc-950")}
                >
                  <Icon aria-hidden className="size-4 text-zinc-500" />
                  {item.label}
                </li>
              );
            })}
          </ul>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

// ------------------------------------------------------------------ shell
export function AppShell({ children }: { children: ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);
  const { path } = useRouter();
  const { data } = useOverview();
  const main = useRef<HTMLElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCommandOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Move focus to the page on navigation, so keyboard and screen-reader users land on the new content.
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    main.current?.focus({ preventScroll: true });
  }, [path]);

  const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

  return (
    <div className="flex min-h-dvh bg-canvas">
      <a href="#main" className="sr-only z-50 rounded bg-white px-3 py-2 focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:ring-2 focus:ring-accent-500">
        Skip to content
      </a>

      <aside className="sticky top-0 hidden h-dvh w-60 shrink-0 border-r border-zinc-200 bg-zinc-50 lg:block">
        <SidebarContent />
      </aside>

      <DialogPrimitive.Root open={mobileOpen} onOpenChange={setMobileOpen}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-zinc-950/30 animate-fade-in lg:hidden" />
          <DialogPrimitive.Content className="fixed inset-y-0 left-0 z-50 w-72 border-r border-zinc-200 bg-zinc-50 shadow-pop focus:outline-none lg:hidden">
            <DialogPrimitive.Title className="sr-only">Navigation</DialogPrimitive.Title>
            <DialogPrimitive.Description className="sr-only">Main navigation</DialogPrimitive.Description>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon" className="absolute right-3 top-3" aria-label="Close navigation">
                <X aria-hidden />
              </Button>
            </DialogPrimitive.Close>
            <SidebarContent onNavigate={() => setMobileOpen(false)} />
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-3 border-b border-zinc-200 bg-white/85 px-4 backdrop-blur supports-[backdrop-filter]:bg-white/75 sm:px-6">
          <Button variant="ghost" size="icon" className="lg:hidden" aria-label="Open navigation" onClick={() => setMobileOpen(true)}>
            <Menu aria-hidden />
          </Button>
          <div className="lg:hidden">
            <Wordmark className="[&>span:last-child]:hidden sm:[&>span:last-child]:inline" />
          </div>
          <div className="hidden min-w-0 items-center gap-2 text-[13px] text-zinc-500 lg:flex">
            <span className="truncate font-medium text-zinc-900">{data?.service.title ?? " "}</span>
            {data && <span className="font-mono text-xs text-zinc-500">v{data.service.version}</span>}
          </div>
          <div className="ml-auto flex items-center gap-1.5 sm:gap-2">
            <button
              type="button"
              onClick={() => setCommandOpen(true)}
              className="hidden h-8 items-center gap-2 rounded-md border border-zinc-200 bg-white pl-2.5 pr-1.5 text-[13px] text-zinc-500 transition-colors hover:border-zinc-300 hover:text-zinc-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 md:flex"
            >
              <Search aria-hidden className="size-3.5" />
              <span className="pr-6">Go to…</span>
              <kbd className="rounded border border-zinc-200 bg-zinc-50 px-1.5 font-sans text-2xs text-zinc-500">{isMac ? "⌘K" : "Ctrl K"}</kbd>
            </button>
            <Button variant="ghost" size="icon" className="md:hidden" aria-label="Go to page" onClick={() => setCommandOpen(true)}>
              <Search aria-hidden />
            </Button>
            <EnvironmentBadge />
            <Notifications />
            <UserMenu />
          </div>
        </header>
        <main id="main" ref={main} tabIndex={-1} className="flex-1 focus:outline-none">
          {children}
        </main>
      </div>
      <CommandMenu open={commandOpen} onOpenChange={setCommandOpen} />
    </div>
  );
}
