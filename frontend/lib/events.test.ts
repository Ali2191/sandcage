import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { appendEvent, isKnown, knownEvents, parseEvent, type LogEvent } from "./events";
import { EVENT_KINDS } from "@contract";

const SAMPLE = readFileSync(path.join(import.meta.dirname, "../../contract/samples/demo-run.jsonl"), "utf8")
  .split("\n")
  .filter((l) => l.trim() !== "");

describe("parseEvent", () => {
  it("parses every line of the contract's sample stream as a known event", () => {
    for (const line of SAMPLE) {
      const r = parseEvent(line);
      expect(r.ok).toBe(true);
      if (r.ok) expect(isKnown(r.event)).toBe(true);
    }
  });

  it("covers every kind in the sample except those the demo does not raise", () => {
    const kinds = new Set(SAMPLE.map((l) => (JSON.parse(l) as { kind: string }).kind));
    const missing = EVENT_KINDS.filter((k) => !kinds.has(k));
    expect(missing).toEqual(["guard", "stopped"]);
  });

  it("keeps an unknown kind without rendering it", () => {
    const r = parseEvent('{"id":5,"kind":"future_thing","run_id":"r","x":1}');
    expect(r).toEqual({ ok: true, event: { id: 5, kind: "future_thing", run_id: "r", unknown: true } });
  });

  it.each([
    ["not json", "not json"],
    ["an array", "[1]"],
    ["no id", '{"kind":"step","run_id":"r"}'],
    ["id zero", '{"id":0,"kind":"step","run_id":"r"}'],
    ["fractional id", '{"id":1.5,"kind":"step","run_id":"r"}'],
    ["string id", '{"id":"1","kind":"step","run_id":"r"}'],
    ["no kind", '{"id":1,"run_id":"r"}'],
    ["no run id", '{"id":1,"kind":"step"}'],
  ])("rejects %s", (_name, data) => {
    expect(parseEvent(data).ok).toBe(false);
  });
});

describe("appendEvent / knownEvents", () => {
  const e = (id: number): LogEvent => ({ id, kind: "x", run_id: "r", unknown: true });

  it("keeps order and drops duplicates", () => {
    let log: LogEvent[] = [];
    for (const id of [1, 2, 2, 4, 3, 1]) log = appendEvent(log, e(id));
    expect(log.map((x) => x.id)).toEqual([1, 2, 3, 4]);
  });

  it("filters out unknown kinds", () => {
    const known = parseEvent(SAMPLE[0] as string);
    if (!known.ok) throw new Error("sample did not parse");
    expect(knownEvents([e(0.5 as number), known.event])).toHaveLength(1);
  });
});
