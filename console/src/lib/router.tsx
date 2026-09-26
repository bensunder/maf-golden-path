// A deliberately tiny router (History API): a dozen static routes don't need a dependency.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type AnchorHTMLAttributes, type ReactNode } from "react";

import { BASE } from "./api";

interface RouterState {
  path: string; // relative to BASE, always starting with "/"
  search: URLSearchParams;
  navigate: (to: string, options?: { replace?: boolean }) => void;
}

const RouterContext = createContext<RouterState | null>(null);

function current(): { path: string; search: URLSearchParams } {
  const full = window.location.pathname;
  const path = full.startsWith(BASE) ? full.slice(BASE.length) || "/" : "/";
  return { path: path.replace(/\/+$/, "") || "/", search: new URLSearchParams(window.location.search) };
}

export function RouterProvider({ children }: { children: ReactNode }) {
  const [location, setLocation] = useState(current);

  useEffect(() => {
    const onPop = () => setLocation(current());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((to: string, options?: { replace?: boolean }) => {
    const url = BASE + (to.startsWith("/") ? to : `/${to}`);
    if (options?.replace) window.history.replaceState(null, "", url);
    else window.history.pushState(null, "", url);
    setLocation(current());
    window.scrollTo({ top: 0 });
  }, []);

  const value = useMemo(() => ({ ...location, navigate }), [location, navigate]);
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

export function useRouter(): RouterState {
  const ctx = useContext(RouterContext);
  if (!ctx) throw new Error("useRouter outside RouterProvider");
  return ctx;
}

export function Link({ to, children, ...rest }: { to: string } & AnchorHTMLAttributes<HTMLAnchorElement>) {
  const { navigate } = useRouter();
  return (
    <a
      href={BASE + to}
      {...rest}
      onClick={(e) => {
        rest.onClick?.(e);
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}
