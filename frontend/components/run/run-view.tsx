"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, getRun, type RunRow } from "@/lib/api";
import { COPY } from "@/lib/copy";
import { isFinished, known } from "@/lib/derive";
import { appendEvent, type LogEvent } from "@/lib/events";
import { streamEvents, type StreamState } from "@/lib/stream";
import { Body, Centered, Main } from "../shell";
import { Skeleton } from "../ui";
import { LiveRun } from "./live";
import { RunReport } from "./report";
import type { ConnectionView } from "./status-pill";

export function RunSkeleton() {
  return (
    <Body>
      <Main>
        <div role="status" aria-label="Loading the run" className="space-y-4">
          <Skeleton className="h-8 w-80" />
          <Skeleton className="h-48" />
        </div>
      </Main>
    </Body>
  );
}

export function RunNotFound({ id }: { id: string }) {
  return (
    <Centered max={512}>
      <div className="pt-16">
        <h1 className="m-0 text-3xl font-bold text-head">{COPY.errors.runNotFound.title}</h1>
        <p className="mt-3 text-muted">
          The API has no run <code className="font-mono text-fg">{id}</code>. It may belong to a different event log.
        </p>
        <Link href="/" className="font-semibold">
          {COPY.errors.runNotFound.link}
        </Link>
      </div>
    </Centered>
  );
}

/** Controller for /runs/{id}: follows the stream, keeps the log, picks Live or Report. */
export function RunView({ runId }: { runId: string }) {
  const [log, setLog] = useState<LogEvent[]>([]);
  const [stream, setStream] = useState<StreamState>({ kind: "connecting" });
  const [info, setInfo] = useState<RunRow | null>(null);
  const [missing, setMissing] = useState(false);
  const [infoLoaded, setInfoLoaded] = useState(false);
  const [released, setReleased] = useState(false);

  const loadInfo = useCallback((): void => {
    getRun(runId).then(
      (row) => {
        setInfo(row);
        setReleased(row.released_by_human === true);
        setInfoLoaded(true);
      },
      (cause: unknown) => {
        if (cause instanceof ApiError && cause.status === 404) setMissing(true);
        // Any other failure: the page still works from the stream, and tries again once it opens.
      },
    );
  }, [runId]);

  useEffect(() => {
    loadInfo();
  }, [loadInfo]);

  useEffect(() => {
    return streamEvents(runId, {
      onEvent: (event) => setLog((prev) => appendEvent(prev, event)),
      onState: (state) => {
        setStream(state);
        if (state.kind === "failed" && state.notFound) setMissing(true);
      },
    });
  }, [runId]);

  useEffect(() => {
    if (stream.kind === "open" && !infoLoaded) loadInfo();
  }, [stream.kind, infoLoaded, loadInfo]);

  const events = useMemo(() => known(log), [log]);
  const finished = isFinished(events);

  // After the run ends its record may have changed (a human release): look again once.
  useEffect(() => {
    if (finished) loadInfo();
  }, [finished, loadInfo]);

  if (missing) return <RunNotFound id={runId} />;
  if (events.length === 0 && stream.kind !== "failed") return <RunSkeleton />;

  const connection: ConnectionView = finished ? "finished" : stream.kind === "failed" || stream.kind === "reconnecting" ? "disconnected" : "live";

  if (finished) {
    return <RunReport runId={runId} events={events} info={info} releasedByHuman={released} onReleased={() => setReleased(true)} />;
  }
  return <LiveRun runId={runId} events={events} info={info} connection={connection} />;
}
