"use client";

import { useCallback, useState } from "react";
import { ApiError, getPatch, getReport, releasePatch, type ReportFormat } from "@/lib/api";
import { COPY } from "@/lib/copy";
import { diffFiles, diffLineKind, type DiffLineKind } from "@/lib/diff";
import { Button, Card, Pill, Skeleton, cx } from "../ui";
import { useLoad } from "../use-load";

type PatchState = { kind: "ready"; text: string } | { kind: "withheld" } | { kind: "error"; message: string };

const LINE_CLASS: Record<DiffLineKind, string> = {
  add: "text-ember-soft",
  remove: "text-faint line-through",
  marker: "text-detail",
  header: "text-detail",
  context: "text-body",
};

function save(filename: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** The "Your patch" card: loading, ready, withheld (with a release gate), or an error. */
export function DeliverPanel({ runId, releasedByHuman, onReleased }: { runId: string; releasedByHuman: boolean; onReleased: () => void }) {
  const [releasing, setReleasing] = useState(false);
  const [releaseError, setReleaseError] = useState<string | null>(null);
  const [format, setFormat] = useState<ReportFormat>("md");
  const [reporting, setReporting] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  // A withheld patch answers 423 and a missing one 409: both are states of the card, not failures.
  const loadPatch = useCallback(async (): Promise<PatchState> => {
    try {
      return { kind: "ready", text: await getPatch(runId) };
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 423) return { kind: "withheld" };
      if (cause instanceof ApiError && cause.status === 409) return { kind: "error", message: COPY.report.patch.notDelivered };
      return { kind: "error", message: cause instanceof ApiError ? cause.message : COPY.live.sendFailed };
    }
  }, [runId]);
  const { state: loaded } = useLoad(loadPatch, COPY.live.sendFailed, String(version));
  const state: PatchState | { kind: "loading" } = loaded.kind === "ready" ? loaded.data : loaded.kind === "error" ? { kind: "error", message: loaded.message } : { kind: "loading" };
  const load = async (): Promise<void> => setVersion((v) => v + 1);

  const release = async (): Promise<void> => {
    setReleasing(true);
    setReleaseError(null);
    try {
      await releasePatch(runId);
      onReleased();
      await load();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        // Already released elsewhere: just reload the card.
        onReleased();
        await load();
      } else setReleaseError(cause instanceof ApiError ? cause.message : COPY.live.sendFailed);
    } finally {
      setReleasing(false);
    }
  };

  const downloadReport = async (): Promise<void> => {
    setReporting(true);
    setReportError(null);
    try {
      save(format === "md" ? "REPORT.md" : "report.json", await getReport(runId, format));
    } catch (cause) {
      setReportError(cause instanceof ApiError ? cause.message : COPY.live.sendFailed);
    } finally {
      setReporting(false);
    }
  };

  const withheld = state.kind === "withheld";
  const files = state.kind === "ready" ? diffFiles(state.text) : [];

  return (
    <Card as="section" aria-labelledby="patch-title">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 id="patch-title" className="m-0 text-base font-semibold text-head">
          {COPY.report.patch.title}
        </h3>
        {withheld && <Pill tone="ember">{COPY.report.patch.blocked}</Pill>}
        {!withheld && releasedByHuman && <Pill tone="ember-soft">{COPY.report.patch.released}</Pill>}
      </div>

      {state.kind === "loading" && <Skeleton label="Loading the patch" className="h-40" />}

      {state.kind === "error" && (
        <div role="alert" className="text-sm text-ember">
          <p className="m-0 mb-2">{state.message}</p>
          <Button size="sm" onClick={() => void load()}>
            {COPY.errors.retry}
          </Button>
        </div>
      )}

      {withheld && (
        <div className="rounded-xl border border-ember-border bg-ember-glow p-4">
          <div className="mb-2 font-semibold text-ember">{COPY.report.patch.withheldTitle}</div>
          <p className="m-0 mb-4 text-sm text-body">{COPY.report.patch.withheld}</p>
          <Button kind="main" busy={releasing} onClick={() => void release()}>
            {COPY.report.patch.release}
          </Button>
          {releaseError && (
            <p role="alert" className="mb-0 mt-2 text-sm text-ember">
              {releaseError}
            </p>
          )}
        </div>
      )}

      {state.kind === "ready" && (
        <>
          {files.length === 0 ? (
            <p className="mb-3 mt-0 text-sm text-muted">{COPY.report.patch.noFiles}</p>
          ) : (
            <ul className="m-0 mb-3 list-none space-y-1.5 p-0">
              {files.map((f) => (
                <li key={f.path} className="flex items-center gap-2 text-sm">
                  <Pill>{f.status}</Pill>
                  <span className="min-w-0 break-all font-mono text-fg">{f.path}</span>
                </li>
              ))}
            </ul>
          )}
          <pre tabIndex={0} aria-label="The patch" className="sc-scroll m-0 mb-4 max-h-64 overflow-auto rounded-xl border border-line bg-night p-3 font-mono text-xs leading-5">
            {state.text.split("\n").map((line, i) => (
              <div key={i} className={cx("whitespace-pre", LINE_CLASS[diffLineKind(line)])}>
                {line === "" ? " " : line}
              </div>
            ))}
          </pre>
          <Button kind="main" className="w-full" onClick={() => save("sandcage.patch", state.text)}>
            {COPY.report.patch.download}
          </Button>
        </>
      )}

      {state.kind !== "loading" && state.kind !== "error" && (
        <div className="mt-3 flex items-center gap-2">
          <Button busy={reporting} className="flex-1" onClick={() => void downloadReport()}>
            {COPY.report.patch.downloadReport}
          </Button>
          <div role="radiogroup" aria-label="Report format" className="flex rounded-full border border-line-strong p-0.5 font-mono text-xs">
            {(["md", "json"] as const).map((f) => (
              <button
                key={f}
                type="button"
                role="radio"
                aria-checked={format === f}
                onClick={() => setFormat(f)}
                className={cx("cursor-pointer rounded-full px-3 py-2 uppercase", format === f ? "bg-raised font-semibold text-fg" : "text-muted hover:text-fg")}
              >
                {f === "md" ? "MD" : "JSON"}
              </button>
            ))}
          </div>
        </div>
      )}
      {reportError && (
        <p role="alert" className="mb-0 mt-2 text-sm text-ember">
          {reportError}
        </p>
      )}
    </Card>
  );
}
