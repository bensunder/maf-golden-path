import { describe, expect, it } from "vitest";

import { initialMemoryState, memoryClient, memoryError, memoryReducer, MemoryApiError, newIdempotencyKey, type MemoryItem, type MemoryState } from "./memory";

const item = (id: string, text: string, updated: number, extra: Partial<MemoryItem> = {}): MemoryItem => ({
  id,
  text,
  kind: "preference",
  source: "agent",
  created_at: updated,
  updated_at: updated,
  expires_at: updated + 86400,
  ...extra,
});

const A = item("mem_aaaaaaaaaaaa", "Prefers bullet points", 100);
const B = item("mem_bbbbbbbbbbbb", "Works in EMEA hours", 200);

function loaded(...items: MemoryItem[]): MemoryState {
  return memoryReducer(initialMemoryState, { type: "loaded", list: { items, count: items.length, max_items: 50, retention_days: 180 } });
}

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

describe("memoryReducer", () => {
  it("loads newest first with the service's limits", () => {
    const s = loaded(A, B);
    expect(s.status).toBe("ready");
    expect(s.items.map((i) => i.id)).toEqual([B.id, A.id]);
    expect([s.maxItems, s.retentionDays]).toEqual([50, 180]);
  });

  it("adds without duplicating a replayed item", () => {
    let s = loaded(A);
    s = memoryReducer(s, { type: "added", item: B });
    s = memoryReducer(s, { type: "added", item: B }); // an idempotent replay returns the same item
    expect(s.items.map((i) => i.id)).toEqual([B.id, A.id]);
    expect(s.announcement).toBe("Remembered.");
  });

  it("removes optimistically and confirms", () => {
    let s = memoryReducer(loaded(A, B), { type: "remove/start", id: A.id });
    expect(s.items.map((i) => i.id)).toEqual([B.id]);
    expect(s.pending[A.id]).toEqual(A);
    s = memoryReducer(s, { type: "remove/done", id: A.id });
    expect(s.pending).toEqual({});
    expect(s.announcement).toBe("Forgotten.");
  });

  it("rolls a refused remove back into place", () => {
    let s = memoryReducer(loaded(A, B), { type: "remove/start", id: A.id });
    s = memoryReducer(s, { type: "remove/undo", id: A.id, error: "Memory is temporarily unavailable." });
    expect(s.items.map((i) => i.id)).toEqual([B.id, A.id]);
    expect(s.pending).toEqual({});
    expect(s.error).toBe("Memory is temporarily unavailable.");
    expect(s.announcement).toContain("Couldn't forget it.");
  });

  it("edits optimistically, then takes the server's version", () => {
    let s = memoryReducer(loaded(A, B), { type: "edit/start", id: A.id, text: "Prefers short replies" });
    expect(s.items.find((i) => i.id === A.id)?.text).toBe("Prefers short replies");
    const saved = { ...A, text: "Prefers short replies", source: "user" as const, updated_at: 300 };
    s = memoryReducer(s, { type: "edit/done", item: saved });
    expect(s.items[0]).toEqual(saved); // now the newest
    expect(s.pending).toEqual({});
  });

  it("rolls a refused edit back to the original text", () => {
    let s = memoryReducer(loaded(A), { type: "edit/start", id: A.id, text: "call me at 555 0100" });
    s = memoryReducer(s, { type: "edit/undo", id: A.id, error: "That looks like personal data." });
    expect(s.items).toEqual([A]);
    expect(s.error).toBe("That looks like personal data.");
  });

  it("ignores actions for items it doesn't have", () => {
    const s = loaded(A);
    expect(memoryReducer(s, { type: "remove/start", id: "mem_000000000000" })).toBe(s);
    expect(memoryReducer(s, { type: "edit/start", id: "mem_000000000000", text: "x" })).toBe(s);
  });

  it("keeps the list on a later failure, but shows an error page on a first-load failure", () => {
    expect(memoryReducer(initialMemoryState, { type: "failed", error: "down" }).status).toBe("error");
    const s = memoryReducer(loaded(A), { type: "failed", error: "down" });
    expect([s.status, s.items.length, s.error]).toEqual(["ready", 1, "down"]);
  });

  it("clears everything", () => {
    const s = memoryReducer(loaded(A, B), { type: "cleared" });
    expect(s.items).toEqual([]);
    expect(s.announcement).toBe("Everything was forgotten.");
  });
});

describe("memoryError", () => {
  it("reads the service's code and message", async () => {
    const e = await memoryError(json(422, { detail: { code: "personal_data", message: "That looks like personal data." } }));
    expect([e.status, e.code, e.message]).toEqual([422, "personal_data", "That looks like personal data."]);
  });

  it("adds Retry-After on 429 and 503", async () => {
    const e = await memoryError(json(429, { detail: { code: "rate_limited", message: "Too many changes." } }, { "Retry-After": "12" }));
    expect(e.retryAfter).toBe(12);
    expect(e.message).toBe("Too many changes. Try again in 12 s.");
  });

  it("reads FastAPI validation errors and falls back on non-JSON bodies", async () => {
    const v = await memoryError(json(422, { detail: [{ msg: "String should have at least 1 character" }] }));
    expect([v.code, v.message]).toEqual(["invalid_request", "String should have at least 1 character"]);
    const plain = await memoryError(new Response("<html>", { status: 401 }));
    expect(plain.code).toBe("http_401");
    expect(plain.message).toContain("not signed in");
  });
});

describe("memoryClient", () => {
  function recorder(responses: Response[]) {
    const calls: { url: string; init?: RequestInit }[] = [];
    const fetcher = async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return responses.shift()!;
    };
    return { calls, client: memoryClient("/agents/pay/v1/memory", fetcher) };
  }

  it("sends adds as JSON with the idempotency key", async () => {
    const { calls, client } = recorder([json(201, A)]);
    expect(await client.add("Prefers bullet points", "key-12345678")).toEqual(A);
    const { url, init } = calls[0];
    const headers = init?.headers as Record<string, string>;
    expect(url).toBe("/agents/pay/v1/memory");
    expect(init?.method).toBe("POST");
    expect(headers["Idempotency-Key"]).toBe("key-12345678");
    expect(headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init?.body as string)).toEqual({ text: "Prefers bullet points", kind: "preference" });
    expect(init?.credentials).toBe("same-origin");
  });

  it("encodes ids and handles 204", async () => {
    const { calls, client } = recorder([new Response(null, { status: 204 }), new Response(null, { status: 204 })]);
    expect(await client.remove("mem_a/b")).toBeUndefined();
    expect(calls[0].url).toBe("/agents/pay/v1/memory/mem_a%2Fb");
    await client.forgetAll();
    expect(calls[1].init?.method).toBe("DELETE");
    expect(client.exportUrl()).toBe("/agents/pay/v1/memory/export");
  });

  it("raises typed errors for HTTP failures and for an unreachable service", async () => {
    const { client } = recorder([json(404, { detail: { code: "not_found", message: "That memory no longer exists." } })]);
    await expect(client.edit(A.id, "x")).rejects.toMatchObject({ status: 404, code: "not_found" });
    const down = memoryClient("/v1/memory", async () => {
      throw new TypeError("Failed to fetch");
    });
    const err = await down.list().catch((e) => e);
    expect(err).toBeInstanceOf(MemoryApiError);
    expect([err.status, err.code]).toEqual([0, "unreachable"]);
  });
});

describe("newIdempotencyKey", () => {
  it("matches the API's key format and is unique", () => {
    const keys = new Set(Array.from({ length: 50 }, newIdempotencyKey));
    expect(keys.size).toBe(50);
    for (const k of keys) expect(k).toMatch(/^[A-Za-z0-9_-]{8,128}$/);
  });
});

describe("refused changes", () => {
  it("keep an empty list on screen instead of an error page, and clear on the next success", () => {
    let s = memoryReducer(loaded(), { type: "rejected", error: "Memory can't hold instructions to the agent.", announce: "Not remembered." });
    expect(s.status).toBe("ready");
    expect(s.error).toBe("Memory can't hold instructions to the agent.");
    expect(s.announcement).toBe("Not remembered. Memory can't hold instructions to the agent.");
    s = memoryReducer(s, { type: "added", item: A });
    expect(s.error).toBeNull();
  });

  it("a failed reload of an empty, loaded list keeps the page", () => {
    const s = memoryReducer(loaded(), { type: "failed", error: "down" });
    expect([s.status, s.error]).toEqual(["ready", "down"]);
  });
});
