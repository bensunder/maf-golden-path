import { describe, expect, it } from "vitest";

import { AGENT_TEXT, toAgentText } from "./api";

describe("toAgentText", () => {
  it("turns a template's one-liner into a description Create agent accepts", () => {
    const vibe = "First point of contact — gathers facts, runs conflict checks, and ensures “smooth” onboarding's {x}.";
    const text = toAgentText(vibe);
    expect(text).toBe("First point of contact, gathers facts, runs conflict checks, and ensures smooth onboardings x.");
    expect(AGENT_TEXT.test(text)).toBe(true);
  });

  it("keeps within the length limit", () => {
    expect(toAgentText("a".repeat(500)).length).toBe(240);
    expect(toAgentText("Corporate M&A Counsel", 60)).toBe("Corporate M&A Counsel");
  });
});
