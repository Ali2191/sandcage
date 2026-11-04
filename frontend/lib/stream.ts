/**
 * Follow a run's event stream with fetch and the SSE parser (not EventSource), resuming
 * from the last event id after a drop. Bounded retries with backoff; three missed
 * 15-second pings (45 s without a byte) count as a dead connection.
 */
import { eventsUrl } from "./api";
import { appendEvent, parseEvent, type LogEvent } from "./events";
import { SseParser } from "./sse";

export type StreamState =
  | { kind: "connecting" }
  | { kind: "open" }
  | { kind: "reconnecting"; attempt: number }
  | { kind: "closed" }
  | { kind: "failed"; message: string; notFound: boolean };

export interface StreamHandlers {
  onEvent(event: LogEvent): void;
  onState(state: StreamState): void;
}

export interface StreamOptions {
  idleMs?: number;
  /** Seconds to wait before retry number n (1-based). */
  backoffMs?: (attempt: number) => number;
  maxFailures?: number;
  fetchImpl?: typeof fetch;
  url?: (runId: string) => string;
}

export const BACKOFF_STEPS_MS: readonly number[] = [500, 1000, 2000, 4000, 8000, 8000];

export function defaultBackoff(attempt: number): number {
  return BACKOFF_STEPS_MS[Math.min(attempt, BACKOFF_STEPS_MS.length) - 1] ?? 8000;
}

class Dead extends Error {}

/**
 * Starts following; returns a function that stops it. `onState` is told about every
 * change; `onEvent` gets each event once, in id order, with duplicates dropped.
 */
export function streamEvents(runId: string, handlers: StreamHandlers, options: StreamOptions = {}): () => void {
  const idleMs = options.idleMs ?? 45_000;
  const backoff = options.backoffMs ?? defaultBackoff;
  const maxFailures = options.maxFailures ?? 6;
  const doFetch = options.fetchImpl ?? ((input, init) => fetch(input, init));
  const urlFor = options.url ?? eventsUrl;
  const controller = new AbortController();
  let log: LogEvent[] = [];
  let lastId = 0;
  let finished = false;

  const deliver = (event: LogEvent): void => {
    if (event.id <= lastId) return;
    lastId = event.id;
    log = appendEvent(log, event);
    handlers.onEvent(event);
    if (event.kind === "done") finished = true;
  };

  const readOnce = async (): Promise<void> => {
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    if (lastId > 0) headers["Last-Event-ID"] = String(lastId);
    const idle = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const arm = (): void => {
      clearTimeout(timer);
      timer = setTimeout(() => idle.abort(), idleMs);
    };
    const onOuterAbort = (): void => idle.abort();
    controller.signal.addEventListener("abort", onOuterAbort);
    arm();
    try {
      const response = await doFetch(urlFor(runId), { headers, signal: idle.signal, cache: "no-store" });
      if (response.status === 404) throw new NotFound();
      if (!response.ok || response.body === null) throw new Dead(`The stream answered ${response.status}.`);
      handlers.onState({ kind: "open" });
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parser = new SseParser();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        arm();
        for (const message of parser.feed(decoder.decode(value, { stream: true }))) {
          const parsed = parseEvent(message.data);
          if (parsed.ok) deliver(parsed.event);
        }
        if (finished) {
          await reader.cancel();
          return;
        }
      }
    } finally {
      clearTimeout(timer);
      controller.signal.removeEventListener("abort", onOuterAbort);
    }
  };

  const run = async (): Promise<void> => {
    handlers.onState({ kind: "connecting" });
    let failures = 0;
    while (!controller.signal.aborted && !finished) {
      const before = lastId;
      try {
        await readOnce();
        if (finished) break;
        failures = lastId > before ? 0 : failures + 1; // closed without a done event
      } catch (cause) {
        if (controller.signal.aborted) return;
        if (cause instanceof NotFound) {
          handlers.onState({ kind: "failed", message: "This run doesn't exist on the server.", notFound: true });
          return;
        }
        failures = lastId > before ? 1 : failures + 1;
      }
      if (failures >= maxFailures) {
        handlers.onState({
          kind: "failed",
          message: "The connection to the run was lost. Reload the page to try again.",
          notFound: false,
        });
        return;
      }
      handlers.onState({ kind: "reconnecting", attempt: failures });
      await sleep(backoff(failures), controller.signal);
    }
    if (finished) handlers.onState({ kind: "closed" });
  };

  void run();
  return () => controller.abort();
}

class NotFound extends Error {}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}
