"use client";

import Link from "next/link";
import { useCallback, useState } from "react";
import { listRuns } from "@/lib/api";
import { COPY } from "@/lib/copy";
import { modelLabel, repoLabel, runMeta, runOutcome } from "@/lib/labels";
import { Button, ErrorCard, Skeleton, cx } from "./ui";
import { useLoad } from "./use-load";

/** The run list: compact (5 rows, no dates, no paging) on Start, 20 per page on Runs. */
export function RecentRuns({ compact = false }: { compact?: boolean }) {
  const pageSize = compact ? 5 : COPY.runs.perPage;
  const [offset, setOffset] = useState(0);
  const load = useCallback(() => listRuns(pageSize, offset), [pageSize, offset]);
  const { state, reload } = useLoad(load, "Could not load the runs.", `${pageSize}:${offset}`);

  if (state.kind === "loading") {
    return (
      <div role="status" aria-label="Loading runs" className="space-y-3">
        <Skeleton className="h-4" />
        <Skeleton className="h-4 w-2/3" />
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <ErrorCard onRetry={reload}>{state.message}</ErrorCard>
    );
  }
  const { runs, total } = state.data;
  if (runs.length === 0) {
    return (
      <p className="m-0 text-[15px] text-muted">
        {COPY.runs.empty} <Link href="/">{COPY.runs.emptyLink}</Link>
        {COPY.runs.emptyTail}
      </p>
    );
  }
  return (
    <div>
      <ul className="m-0 list-none p-0">
        {runs.map((row) => {
          const outcome = runOutcome(row);
          return (
            <li key={row.id} className="border-b border-line-soft last:border-b-0">
              <Link href={`/runs/${row.id}`} className="flex items-center justify-between gap-4 py-3 no-underline">
                <span className="min-w-0">
                  <span className="block truncate text-sm text-fg">{repoLabel(row.repo)}</span>
                  <span className="block truncate font-mono text-xs text-faint" title={modelLabel(row) ?? undefined}>
                    {runMeta(row, !compact)}
                  </span>
                </span>
                <span className={cx("shrink-0 text-sm", outcome.tone === "ember" ? "font-semibold text-ember" : outcome.tone === "muted" ? "text-muted" : "text-body")}>
                  {outcome.text}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
      {!compact && total > pageSize && (
        <div className="mt-4 flex items-center justify-between gap-3">
          <Button size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>
            {COPY.runs.newer}
          </Button>
          <span className="text-sm text-muted" aria-live="polite">
            {offset + 1}–{Math.min(total, offset + pageSize)} of {total}
          </span>
          <Button size="sm" disabled={offset + pageSize >= total} onClick={() => setOffset(offset + pageSize)}>
            {COPY.runs.older}
          </Button>
        </div>
      )}
    </div>
  );
}

