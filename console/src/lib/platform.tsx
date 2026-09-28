// The VPS platform, when this console is served behind it: every agent on the server, and Create agent
// that builds and launches one. Anywhere else the platform API is absent and these return null.

import { useEffect, useState } from "react";

import { ApiError, PLATFORM, platformApi, type PlatformAgent, type PlatformInfo, type PlatformJob } from "./api";
import { useLoad, type Loadable } from "./data";

export function usePlatform(): { info: PlatformInfo | null; loading: boolean } {
  const { data, loading } = useLoad(() =>
    PLATFORM ? platformApi.info().catch((e) => (e instanceof ApiError && e.status !== 0 ? null : Promise.reject(e))) : Promise.resolve(null),
  );
  return { info: data ?? null, loading };
}

/** Every agent on the server, re-read every few seconds while one is being built. */
export function usePlatformAgents(enabled: boolean): Loadable<{ agents: PlatformAgent[]; jobs: PlatformJob[] }> {
  const state = useLoad(() => (enabled ? platformApi.agents() : Promise.resolve({ agents: [], jobs: [] })), [enabled]);
  const busy = Boolean(state.data?.jobs.length);
  const { reload } = state;
  useEffect(() => {
    if (!enabled) return;
    const timer = window.setInterval(reload, busy ? 3000 : 15000);
    return () => window.clearInterval(timer);
  }, [enabled, busy, reload]);
  return state;
}

/** One creation, polled until it's ready or failed. */
export function useJob(id: string | null): { job: PlatformJob | null; error: ApiError | null } {
  const [job, setJob] = useState<PlatformJob | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => {
    if (!id) return;
    let alive = true;
    let timer = 0;
    const tick = async () => {
      try {
        const next = await platformApi.job(id);
        if (!alive) return;
        setJob(next);
        setError(null);
        if (next.state !== "ready" && next.state !== "failed") timer = window.setTimeout(tick, 1500);
      } catch (err) {
        if (!alive) return;
        setError(err instanceof ApiError ? err : new ApiError(0, "Something went wrong."));
        timer = window.setTimeout(tick, 4000);
      }
    };
    void tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [id]);
  return { job, error };
}

export const STEPS: { state: PlatformJob["state"]; label: string; detail: string }[] = [
  { state: "generating", label: "Generate from the template", detail: "A new MAF agent project: tools, instructions, evals, tests." },
  { state: "registering", label: "Register on this server", detail: "Its own path, sessions database and fleet entry." },
  { state: "building", label: "Build and run the evals", detail: "The build fails if the quality gate fails." },
  { state: "starting", label: "Start", detail: "Wait until it answers its health check." },
  { state: "ready", label: "Ready", detail: "Open its console and chat." },
];

export function stepIndex(state: PlatformJob["state"]): number {
  const i = STEPS.findIndex((s) => s.state === state);
  return state === "queued" ? -1 : i;
}
