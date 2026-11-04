import { describe, expect, it } from "vitest";
import { streamEvents, defaultBackoff, type StreamState } from "./stream";
import type { LogEvent } from "./events";

const enc = new TextEncoder();

const message = (id: number, kind = "step"): string =>
  `id: ${id}\nevent: ${kind}\ndata: ${JSON.stringify({ id, kind, run_id: "r1" })}\n\n`;

/** A response whose body yields the given chunks, then either ends or hangs until aborted. */
function respond(chunks: string[], opts: { hang?: boolean; signal?: AbortSignal; status?: number } = {}): Response {
  const body = new ReadableStream<Uint8Array>({
    async start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      if (!opts.hang) controller.close();
      else
        await new Promise<void>((resolve) => {
          opts.signal?.addEventListener("abort", () => {
            controller.error(new DOMException("aborted", "AbortError"));
            resolve();
          });
        });
    },
  });
  return new Response(body, { status: opts.status ?? 200, headers: { "Content-Type": "text/event-stream" } });
}

interface Harness {
  events: LogEvent[];
  states: StreamState[];
  requests: { lastEventId: string | null }[];
  stop: () => void;
  settled: Promise<void>;
}

function follow(script: ((signal: AbortSignal) => Response | Error)[], extra: { idleMs?: number; maxFailures?: number } = {}): Harness {
  const events: LogEvent[] = [];
  const states: StreamState[] = [];
  const requests: { lastEventId: string | null }[] = [];
  let call = 0;
  let resolveSettled: () => void = () => undefined;
  const settled = new Promise<void>((r) => (resolveSettled = r));
  const fetchImpl: typeof fetch = async (_input, init) => {
    const headers = new Headers(init?.headers);
    requests.push({ lastEventId: headers.get("Last-Event-ID") });
    const step = script[Math.min(call, script.length - 1)];
    call += 1;
    const out = step?.(init?.signal as AbortSignal);
    if (out instanceof Error) throw out;
    return out as Response;
  };
  const stop = streamEvents(
    "r1",
    {
      onEvent: (e) => events.push(e),
      onState: (s) => {
        states.push(s);
        if (s.kind === "closed" || s.kind === "failed") resolveSettled();
      },
    },
    { fetchImpl, backoffMs: () => 1, url: () => "http://x/events", ...extra },
  );
  return { events, states, requests, stop, settled };
}

describe("streamEvents", () => {
  it("delivers events in order and closes after done", async () => {
    const h = follow([() => respond([message(1), message(2), message(3, "done")])]);
    await h.settled;
    expect(h.events.map((e) => e.id)).toEqual([1, 2, 3]);
    expect(h.states.at(-1)?.kind).toBe("closed");
    expect(h.requests).toHaveLength(1);
    expect(h.requests[0]?.lastEventId).toBeNull();
  });

  it("handles events split across network chunks", async () => {
    const wire = message(1) + message(2, "done");
    const h = follow([() => respond([wire.slice(0, 17), wire.slice(17, 60), wire.slice(60)])]);
    await h.settled;
    expect(h.events.map((e) => e.id)).toEqual([1, 2]);
  });

  it("resumes with Last-Event-ID after a drop and never repeats an event", async () => {
    const h = follow([
      () => respond([message(1), message(2)]), // closes without done
      () => respond([message(2), message(3), message(4, "done")]), // server replays 2
    ]);
    await h.settled;
    expect(h.events.map((e) => e.id)).toEqual([1, 2, 3, 4]);
    expect(h.requests.map((r) => r.lastEventId)).toEqual([null, "2"]);
    expect(h.states.some((s) => s.kind === "reconnecting")).toBe(true);
  });

  it("resumes after a network error too", async () => {
    const h = follow([() => respond([message(1)]), () => new TypeError("network down"), () => respond([message(2, "done")])]);
    await h.settled;
    expect(h.events.map((e) => e.id)).toEqual([1, 2]);
  });

  it("keeps unknown kinds in the log but flags them", async () => {
    const h = follow([() => respond([message(1, "from_the_future"), message(2, "done")])]);
    await h.settled;
    expect(h.events[0]).toMatchObject({ kind: "from_the_future", unknown: true });
  });

  it("skips a malformed payload and carries on", async () => {
    const h = follow([() => respond(["id: 1\ndata: not json\n\n", message(2, "done")])]);
    await h.settled;
    expect(h.events.map((e) => e.id)).toEqual([2]);
  });

  it("reports a missing run as not found without retrying", async () => {
    const h = follow([() => respond([], { status: 404 })]);
    await h.settled;
    expect(h.states.at(-1)).toEqual({ kind: "failed", message: "This run doesn't exist on the server.", notFound: true });
    expect(h.requests).toHaveLength(1);
  });

  it("gives up after the failure limit when no progress is made", async () => {
    const h = follow([() => new TypeError("down")], { maxFailures: 3 });
    await h.settled;
    expect(h.states.at(-1)).toMatchObject({ kind: "failed", notFound: false });
    expect(h.requests).toHaveLength(3);
  });

  it("treats a silent connection as dead once the idle limit passes, then resumes", async () => {
    const h = follow([(signal) => respond([message(1)], { hang: true, signal }), () => respond([message(2, "done")])], { idleMs: 30 });
    await h.settled;
    expect(h.events.map((e) => e.id)).toEqual([1, 2]);
    expect(h.requests.map((r) => r.lastEventId)).toEqual([null, "1"]);
  });

  it("stops when asked and does not reconnect", async () => {
    const h = follow([(signal) => respond([message(1)], { hang: true, signal })]);
    await new Promise((r) => setTimeout(r, 20));
    h.stop();
    await new Promise((r) => setTimeout(r, 20));
    expect(h.requests).toHaveLength(1);
    expect(h.states.some((s) => s.kind === "failed")).toBe(false);
  });
});

describe("defaultBackoff", () => {
  it("steps 0.5, 1, 2, 4, 8, 8 seconds", () => {
    expect([1, 2, 3, 4, 5, 6, 7].map(defaultBackoff)).toEqual([500, 1000, 2000, 4000, 8000, 8000, 8000]);
  });
});
