import { describe, expect, it } from "vitest";

import { SseParser } from "./agui";
import { humanize, formatValue, shortId } from "./format";

describe("SseParser", () => {
  it("parses events split across chunks", () => {
    const p = new SseParser();
    expect(p.push('data: {"type":"RUN_STA')).toEqual([]);
    expect(p.push('RTED","threadId":"t","runId":"r"}\n\ndata: {"type":"TEXT_MESSAGE_CONTENT","messageId":"m","delta":"hi"}\n\n'))
      .toEqual([
        { type: "RUN_STARTED", threadId: "t", runId: "r" },
        { type: "TEXT_MESSAGE_CONTENT", messageId: "m", delta: "hi" },
      ]);
  });

  it("handles CRLF, event lines and junk frames", () => {
    const p = new SseParser();
    const events = p.push('event: x\r\ndata: {"type":"RUN_FINISHED"}\r\n\r\ndata: not json\n\n: comment\n\n');
    expect(events).toEqual([{ type: "RUN_FINISHED" }]);
  });
});

describe("format", () => {
  it("humanizes identifiers", () => {
    expect(humanize("issue_refund")).toBe("Issue refund");
    expect(humanize("order_id")).toBe("Order ID");
    expect(humanize("lookupOrder")).toBe("Lookup order");
  });

  it("formats argument values as text", () => {
    expect(formatValue("A1002")).toBe("A1002");
    expect(formatValue(129)).toBe("129");
    expect(formatValue({ a: 1 })).toBe('{"a":1}');
    expect(formatValue(null)).toBe("—");
  });

  it("shortens ids", () => {
    expect(shortId("agui-0b8f3c2a-1111-2222")).toBe("0b8f3c2a");
    expect(shortId("abc")).toBe("abc");
  });
});
