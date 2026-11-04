import { describe, expect, it } from "vitest";
import { SseParser } from "./sse";

describe("SseParser", () => {
  it("reads id, event and data", () => {
    const p = new SseParser();
    expect(p.feed("id: 7\nevent: step\ndata: {\"a\":1}\n\n")).toEqual([
      { id: "7", event: "step", data: '{"a":1}' },
    ]);
  });

  it("defaults the event name to message", () => {
    expect(new SseParser().feed("data: hi\n\n")).toEqual([{ id: null, event: "message", data: "hi" }]);
  });

  it("ignores comment lines such as the keep-alive ping", () => {
    const p = new SseParser();
    expect(p.feed(": ping\n\n")).toEqual([]);
    expect(p.feed(": ping\nid: 1\ndata: x\n\n")).toHaveLength(1);
  });

  it("joins multi-line data with a newline", () => {
    expect(new SseParser().feed("data: a\ndata: b\n\n")[0]?.data).toBe("a\nb");
  });

  it("handles a message split across chunks at any point", () => {
    const wire = "id: 3\nevent: breaker\ndata: {\"x\":\"é\"}\n\n";
    for (let cut = 1; cut < wire.length; cut += 1) {
      const p = new SseParser();
      const out = [...p.feed(wire.slice(0, cut)), ...p.feed(wire.slice(cut))];
      expect(out).toEqual([{ id: "3", event: "breaker", data: '{"x":"é"}' }]);
    }
  });

  it("handles CRLF, including a CR LF split across chunks", () => {
    const p = new SseParser();
    const out = [...p.feed("id: 1\r"), ...p.feed("\ndata: a\r\n\r"), ...p.feed("\n")];
    expect(out).toEqual([{ id: "1", event: "message", data: "a" }]);
  });

  it("handles lone CR line breaks", () => {
    expect(new SseParser().feed("data: a\r\r")).toEqual([{ id: null, event: "message", data: "a" }]);
  });

  it("keeps the last id for following messages and exposes it", () => {
    const p = new SseParser();
    const out = p.feed("id: 4\ndata: a\n\ndata: b\n\n");
    expect(out.map((m) => m.id)).toEqual(["4", "4"]);
    expect(p.lastEventId).toBe("4");
  });

  it("does not dispatch a block with no data", () => {
    expect(new SseParser().feed("id: 9\nevent: x\n\n")).toEqual([]);
  });

  it("strips exactly one leading space from a value", () => {
    expect(new SseParser().feed("data:  two\n\n")[0]?.data).toBe(" two");
    expect(new SseParser().feed("data:none\n\n")[0]?.data).toBe("none");
  });

  it("resets the event name between messages", () => {
    const out = new SseParser().feed("event: a\ndata: 1\n\ndata: 2\n\n");
    expect(out.map((m) => m.event)).toEqual(["a", "message"]);
  });

  it("ignores unknown fields and retry", () => {
    expect(new SseParser().feed("retry: 1000\nfoo: bar\ndata: z\n\n")).toHaveLength(1);
  });
});
