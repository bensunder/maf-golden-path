import { describe, expect, it } from "vitest";

import type { Control } from "./api";
import { FRAMEWORKS, MAPPING, REFERENCES, coverage, coverageSummary, gateControl } from "./nist";

const control = (id: string, status: Control["status"]): Control => ({ id, name: id, status, detail: "" });

describe("NIST coverage", () => {
  it("maps only to references the catalog defines", () => {
    for (const [id, mapping] of Object.entries(MAPPING)) {
      for (const [fw, refs] of Object.entries(mapping)) {
        for (const ref of refs ?? []) expect(REFERENCES[fw as keyof typeof REFERENCES], `${id} → ${fw} ${ref}`).toHaveProperty([ref]);
      }
    }
    expect(FRAMEWORKS.map((f) => f.id)).toEqual(["ai_rmf", "ai_600_1", "csf_2", "sp_800_53"]);
  });

  it("every reference is supported by at least one control", () => {
    for (const fw of FRAMEWORKS) {
      for (const ref of Object.keys(REFERENCES[fw.id])) {
        expect(Object.values(MAPPING).some((m) => (m[fw.id] ?? []).includes(ref)), `${fw.id} ${ref}`).toBe(true);
      }
    }
  });

  it("combines the statuses of the controls behind a reference", () => {
    const rows = coverage("sp_800_53", [control("prompt_injection", "partial"), control("tool_output", "on"), control("pii", "on")]);
    const byRef = Object.fromEntries(rows.map((r) => [r.ref, r]));
    expect(byRef["SI-10"].status).toBe("partial"); // heuristic input check + scanned tool output
    expect(byRef["SI-19"].status).toBe("on");
    expect(byRef["AC-5"].status).toBe("none"); // nothing on this agent evidences it
    expect(byRef["SI-10"].evidence.map((c) => c.id)).toEqual(["prompt_injection", "tool_output"]);
    expect(coverageSummary(rows)).toMatchObject({ on: 2, partial: 1 }); // SI-15 and SI-19 on; SI-10 partial
  });

  it("counts the quality gate as evidence", () => {
    expect(gateControl({ passed: true, live: false, pass_rate: 1 }).status).toBe("on");
    expect(gateControl({ passed: false, live: true, pass_rate: 0.5 }).status).toBe("partial");
    expect(gateControl(null).status).toBe("off");
    const rows = coverage("ai_rmf", [gateControl({ passed: true, live: true, pass_rate: 0.95 })]);
    expect(rows.find((r) => r.ref === "MEASURE 2.1")?.status).toBe("on");
  });
});
