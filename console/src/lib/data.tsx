// Shared data: the service overview (fetched once, reloadable) and the sessions this browser started.
//
// The service has no "list everything" endpoint on purpose: sessions are private to their user. The
// console remembers the ids of sessions started here (ids only, never message text) and asks the
// service for each one's state, with the same ownership checks as the JSON API.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError, api, type Overview, type SessionInfo } from "./api";

// ------------------------------------------------------------------ generic loader
export interface Loadable<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
}

export function useLoad<T>(load: () => Promise<T>, deps: unknown[] = []): Loadable<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    load()
      .then((value) => alive && (setData(value), setError(null)))
      .catch((err) => alive && setError(err instanceof ApiError ? err : new ApiError(0, "Something went wrong.")))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, ...deps]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}

// ------------------------------------------------------------------ overview
const OverviewContext = createContext<Loadable<Overview> | null>(null);

export function OverviewProvider({ children }: { children: ReactNode }) {
  const value = useLoad(api.overview);
  return <OverviewContext.Provider value={value}>{children}</OverviewContext.Provider>;
}

export function useOverview(): Loadable<Overview> {
  const ctx = useContext(OverviewContext);
  if (!ctx) throw new Error("useOverview outside OverviewProvider");
  return ctx;
}

// ------------------------------------------------------------------ known sessions
export interface KnownSession {
  id: string; // server session id, e.g. agui-<thread>
  thread?: string; // AG-UI thread id when started in the playground
  created: number; // epoch seconds: when the conversation started here, or when it was opened by ID
  source: "playground" | "lookup";
}

const STORAGE_KEY = "agentkit.console.sessions";
const MAX_KNOWN = 50;

function readKnown(): KnownSession[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((s) => s && typeof s.id === "string") : [];
  } catch {
    return [];
  }
}

function writeKnown(list: KnownSession[]) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(list.slice(0, MAX_KNOWN)));
  } catch {
    /* storage blocked: the list lives in memory for this tab */
  }
}

export type SessionState =
  | { status: "loading" }
  | { status: "ok"; info: SessionInfo }
  | { status: "gone" } // expired or deleted
  | { status: "denied"; message: string }
  | { status: "error"; message: string };

interface SessionsValue {
  known: KnownSession[];
  states: Record<string, SessionState>;
  remember: (session: KnownSession) => void;
  forget: (id: string) => void;
  refresh: (id?: string) => Promise<void>;
  pendingCount: number;
  refreshedAt: number | null;
}

const SessionsContext = createContext<SessionsValue | null>(null);

export function SessionsProvider({ children }: { children: ReactNode }) {
  const [known, setKnown] = useState<KnownSession[]>(readKnown);
  const [states, setStates] = useState<Record<string, SessionState>>({});
  const [refreshedAt, setRefreshedAt] = useState<number | null>(null);
  const knownRef = useRef(known);
  knownRef.current = known;
  const statesRef = useRef(states);
  statesRef.current = states;

  const fetchOne = useCallback(async (id: string) => {
    try {
      const info = await api.session(id);
      setStates((s) => ({ ...s, [id]: { status: "ok", info } }));
    } catch (err) {
      const e = err instanceof ApiError ? err : new ApiError(0, "Something went wrong.");
      const state: SessionState =
        e.status === 404 ? { status: "gone" } : e.status === 403 ? { status: "denied", message: e.message } : { status: "error", message: e.message };
      setStates((s) => ({ ...s, [id]: state }));
    }
  }, []);

  const refresh = useCallback(
    async (id?: string) => {
      // Expired or foreign sessions don't come back; stop asking about them (Refresh on one still re-checks).
      const ids = id
        ? [id]
        : knownRef.current.map((s) => s.id).filter((sid) => !["gone", "denied"].includes(statesRef.current[sid]?.status ?? ""));
      await Promise.all(ids.map(fetchOne));
      setRefreshedAt(Date.now());
    },
    [fetchOne],
  );

  const remember = useCallback(
    (session: KnownSession) => {
      setKnown((list) => {
        const next = [session, ...list.filter((s) => s.id !== session.id)];
        writeKnown(next);
        return next;
      });
      setStates((s) => ({ ...s, [session.id]: s[session.id] ?? { status: "loading" } }));
      void fetchOne(session.id);
    },
    [fetchOne],
  );

  const forget = useCallback((id: string) => {
    setKnown((list) => {
      const next = list.filter((s) => s.id !== id);
      writeKnown(next);
      return next;
    });
    setStates((s) => {
      const { [id]: _removed, ...rest } = s;
      return rest;
    });
  }, []);

  // First load, then every 20 s while the tab is visible (approvals decided elsewhere show up).
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, 20_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const pendingCount = useMemo(
    () =>
      Object.values(states).reduce((n, s) => n + (s.status === "ok" ? s.info.pending.length : 0), 0),
    [states],
  );

  const value = useMemo(
    () => ({ known, states, remember, forget, refresh, pendingCount, refreshedAt }),
    [known, states, remember, forget, refresh, pendingCount, refreshedAt],
  );
  return <SessionsContext.Provider value={value}>{children}</SessionsContext.Provider>;
}

export function useSessions(): SessionsValue {
  const ctx = useContext(SessionsContext);
  if (!ctx) throw new Error("useSessions outside SessionsProvider");
  return ctx;
}
