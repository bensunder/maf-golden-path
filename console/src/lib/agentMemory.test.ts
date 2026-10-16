import { describe, expect, it } from "vitest";

import { SUBJECT_PATTERN, agentMemoryClient, humanizeEvent, lessonsIn, revertOptions, upsertLesson, type Lesson } from "./agentMemory";
import { MemoryApiError } from "./memory";

const lesson = (id: string, text: string, status: Lesson["status"], updated: number, history: Lesson["history"] = []): Lesson => ({
  id,
  text,
  status,
  origin: "user",
  created_at: updated,
  updated_at: updated,
  expires_at: updated + 86400,
  run_id: null,
  history,
});

function json(status: number, body: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function recorder(response: Response = json(200, {})) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const fetcher = async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return response.clone();
  };
  return { calls, fetcher };
}

describe("agentMemoryClient", () => {
  it("talks to /agent under the memory API, with JSON bodies on every POST", async () => {
    const { calls, fetcher } = recorder();
    const client = agentMemoryClient("/agents/legal/v1/memory/", fetcher);
    await client.approve("les_aaaaaaaaaaaa");
    await client.revert("les_aaaaaaaaaaaa", 2);
    await client.addLesson("Code 51 means insufficient funds");
    expect(calls.map((c) => [c.init?.method, c.url])).toEqual([
      ["POST", "/agents/legal/v1/memory/agent/lessons/les_aaaaaaaaaaaa/approve"],
      ["POST", "/agents/legal/v1/memory/agent/lessons/les_aaaaaaaaaaaa/revert"],
      ["POST", "/agents/legal/v1/memory/agent/lessons"],
    ]);
    for (const c of calls) expect((c.init?.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(calls.map((c) => c.init?.body)).toEqual(["{}", '{"revision":2}', '{"text":"Code 51 means insufficient funds"}']);
  });

  it("encodes subjects and journal filters", async () => {
    const { calls, fetcher } = recorder();
    const client = agentMemoryClient("/v1/memory", fetcher);
    await client.subject("invoice:INV-0042");
    await client.journal(72, "customer:1042");
    await client.journal();
    expect(calls.map((c) => c.url)).toEqual([
      "/v1/memory/agent/subjects/invoice%3AINV-0042",
      "/v1/memory/agent/journal?hours=72&subject=customer%3A1042",
      "/v1/memory/agent/journal?hours=24",
    ]);
  });

  it("turns refusals into errors with the service's code and message", async () => {
    const { fetcher } = recorder(json(403, { detail: { code: "not_memory_admin", message: "Only a memory admin can see this." } }));
    const err = await agentMemoryClient("/v1/memory", fetcher)
      .lessons()
      .catch((e) => e);
    expect(err).toBeInstanceOf(MemoryApiError);
    expect([err.status, err.code, err.message]).toEqual([403, "not_memory_admin", "Only a memory admin can see this."]);
  });

  it("reports an unreachable service as such", async () => {
    const client = agentMemoryClient("/v1/memory", async () => {
      throw new TypeError("network");
    });
    const err = await client.summary().catch((e) => e);
    expect([err.status, err.code]).toEqual([0, "unreachable"]);
  });

  it("returns nothing for 204", async () => {
    const { fetcher } = recorder(new Response(null, { status: 204 }));
    await expect(agentMemoryClient("/v1/memory", fetcher).archive("les_aaaaaaaaaaaa")).resolves.toBeUndefined();
  });
});

describe("helpers", () => {
  const A = lesson("les_aaaaaaaaaaaa", "A", "active", 100);
  const B = lesson("les_bbbbbbbbbbbb", "B", "pending", 200);
  const C = lesson("les_cccccccccccc", "C", "active", 300);

  it("filters by status, newest first, and replaces changed lessons", () => {
    expect(lessonsIn([A, B, C], "active").map((l) => l.id)).toEqual([C.id, A.id]);
    const approved = { ...B, status: "active" as const, updated_at: 400 };
    const list = upsertLesson([A, B, C], approved);
    expect(list).toHaveLength(3);
    expect(lessonsIn(list, "active").map((l) => l.id)).toEqual([B.id, C.id, A.id]);
  });

  it("offers each earlier text once, newest first, never the current one", () => {
    const l = lesson("les_dddddddddddd", "v3", "active", 9, [
      { action: "proposed", text: "v1", at: 1, by: "u" },
      { action: "edited", text: "v2", at: 2, by: "u" },
      { action: "edited", text: "v1", at: 3, by: "u" },
      { action: "edited", text: "v3", at: 4, by: "u" },
    ]);
    expect(revertOptions(l)).toEqual([
      { revision: 2, text: "v1", at: 3 },
      { revision: 1, text: "v2", at: 2 },
    ]);
  });

  it("matches the service's subject format", () => {
    expect(["customer:1042", "invoice:INV-2026.01_a"].every((s) => SUBJECT_PATTERN.test(s))).toBe(true);
    expect(["Adam Cross", "customer:", "customer:a@b.com", "Customer:1"].some((s) => SUBJECT_PATTERN.test(s))).toBe(false);
  });

  it("reads events as words", () => {
    expect(humanizeEvent("recovery_draft_created")).toBe("Recovery draft created");
  });
});
