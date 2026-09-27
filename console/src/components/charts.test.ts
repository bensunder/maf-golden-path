import { describe, expect, it } from "vitest";

import { compact, fillBins } from "./charts";

describe("fillBins", () => {
  it("includes the partial oldest bin and marks both ends partial", () => {
    const now = Date.UTC(2026, 8, 27, 10, 37);
    const rows = [
      { t: "2026-09-26T10:00:00Z", v: 1 },
      { t: "2026-09-27T10:00:00Z", v: 2 },
    ];
    const out = fillBins(rows, 60, 1440, now);
    expect(out.length).toBe(25); // 10:00 yesterday .. 10:00 today
    expect(out[0]).toEqual(rows[0]);
    expect(out[24]).toEqual(rows[1]);
    expect(out.slice(1, 24).every((r) => r === null)).toBe(true);
    expect(out.partial![0]).toBe(true);
    expect(out.partial![24]).toBe(true);
    expect(out.partial!.slice(1, 24).every((p) => !p)).toBe(true);
  });

  it("aligns 6-hour bins to UTC like KQL bin()", () => {
    const now = Date.UTC(2026, 8, 27, 10, 37);
    const out = fillBins([], 360, 10080, now);
    expect(new Date(out.times![out.times!.length - 1]).toISOString()).toBe("2026-09-27T06:00:00.000Z");
    expect(new Date(out.times![0]).toISOString()).toBe("2026-09-20T06:00:00.000Z");
    expect(out.length).toBe(29);
  });
});

describe("compact", () => {
  it("drops trailing zeros", () => {
    expect(compact(2.5)).toBe("2.5");
    expect(compact(1500)).toBe("1.5k");
    expect(compact(12)).toBe("12");
  });
});
