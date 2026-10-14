// The Memory page's controller: loads the caller's memory once, then applies edits and deletes
// optimistically (the list changes at once, and rolls back if the service refuses).

import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";

import { initialMemoryState, memoryClient, memoryReducer, newIdempotencyKey, type MemoryClient, type MemoryState } from "./memory";

export interface MemoryController {
  state: MemoryState;
  reload: () => void;
  add: (text: string) => Promise<boolean>;
  edit: (id: string, text: string) => Promise<boolean>;
  remove: (id: string) => Promise<void>;
  forgetAll: () => Promise<boolean>;
  exportUrl: string;
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong.";
}

export function useMemory(base: string | null, client?: MemoryClient): MemoryController {
  const api = useMemo(() => client ?? memoryClient(base ?? undefined), [base, client]);
  const [state, dispatch] = useReducer(memoryReducer, initialMemoryState);
  // One idempotency key per text being added, reused if the person presses Add again after a failure.
  const addKeys = useRef(new Map<string, string>());

  const reload = useCallback(() => {
    let alive = true;
    dispatch({ type: "loading" });
    api
      .list()
      .then((list) => alive && dispatch({ type: "loaded", list }))
      .catch((err) => alive && dispatch({ type: "failed", error: message(err) }));
    return () => {
      alive = false;
    };
  }, [api]);

  useEffect(() => reload(), [reload]);

  const add = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return false;
      const key = addKeys.current.get(trimmed) ?? newIdempotencyKey();
      addKeys.current.set(trimmed, key);
      try {
        const item = await api.add(trimmed, key);
        addKeys.current.delete(trimmed);
        dispatch({ type: "added", item });
        return true;
      } catch (err) {
        dispatch({ type: "rejected", error: message(err), announce: "Not remembered." });
        return false;
      }
    },
    [api],
  );

  const edit = useCallback(
    async (id: string, text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return false;
      dispatch({ type: "edit/start", id, text: trimmed });
      try {
        dispatch({ type: "edit/done", item: await api.edit(id, trimmed) });
        return true;
      } catch (err) {
        dispatch({ type: "edit/undo", id, error: message(err) });
        return false;
      }
    },
    [api],
  );

  const remove = useCallback(
    async (id: string) => {
      dispatch({ type: "remove/start", id });
      try {
        await api.remove(id);
        dispatch({ type: "remove/done", id });
      } catch (err) {
        dispatch({ type: "remove/undo", id, error: message(err) });
      }
    },
    [api],
  );

  const forgetAll = useCallback(async () => {
    try {
      await api.forgetAll();
      dispatch({ type: "cleared" });
      return true;
    } catch (err) {
      dispatch({ type: "rejected", error: message(err), announce: "Couldn't forget everything." });
      return false;
    }
  }, [api]);

  return { state, reload, add, edit, remove, forgetAll, exportUrl: api.exportUrl() };
}
