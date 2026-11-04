"use client";

import { getBenchResults, isRealBench, type BenchResults } from "@/lib/api";
import { categoriesIn, modeRates } from "@/lib/bench";
import { COPY } from "@/lib/copy";
import { Card, Empty, ErrorCard, Skeleton, cx } from "../ui";
import { useLoad } from "../use-load";

const pct = (v: number | null): string => (v === null ? "–" : `${v}%`);

export function Bench() {
  const { state, reload } = useLoad(getBenchResults, "Could not load the Bench results.");

  return (
    <>
      <h1 className="m-0 text-[34px] font-bold tracking-tight text-head">{COPY.bench.headline}</h1>
      <p className="mb-7 mt-3 max-w-[62ch] text-[17px] text-muted">{COPY.bench.sub}</p>
      {state.kind === "loading" && <Skeleton label="Loading the Bench results" className="h-64" />}
      {state.kind === "error" && <ErrorCard onRetry={reload}>{state.message}</ErrorCard>}
      {state.kind === "ready" && state.data === null && <Empty>{COPY.bench.empty}</Empty>}
      {state.kind === "ready" && state.data !== null && !isRealBench(state.data) && <Empty>{COPY.bench.scriptedOnly}</Empty>}
      {state.kind === "ready" && isRealBench(state.data) && <RealResults results={state.data} />}
    </>
  );
}

function RealResults({ results }: { results: BenchResults & { model: string } }) {
  const base = modeRates(results, "baseline");
  const full = modeRates(results, "full");
  const categories = categoriesIn(results);
  return (
    <div className="space-y-5">
      <Card as="section" aria-labelledby="bench-headline">
        <h2 id="bench-headline" className="m-0 mb-1 text-lg font-semibold text-head">
          Headline
        </h2>
        <p className="m-0 mb-4 text-sm text-muted">
          {results.scenarios.length} scenarios · model {results.model}
          {results.date ? ` · ${results.date}` : ""}
        </p>
        <table className="w-full border-collapse text-left text-[15px]">
          <thead>
            <tr className="border-b border-line text-muted">
              <th scope="col" className="py-2 font-semibold">Metric</th>
              <th scope="col" className="py-2 font-semibold">Baseline</th>
              <th scope="col" className="py-2 font-semibold">Full</th>
            </tr>
          </thead>
          <tbody>
            <tr className="border-b border-line-soft">
              <th scope="row" className="py-2.5 font-normal">Attack success rate</th>
              <td className={cx("py-2.5 font-mono", (base.attackSuccess ?? 0) > 50 && "text-ember")}>{pct(base.attackSuccess)}</td>
              <td className={cx("py-2.5 font-mono", (full.attackSuccess ?? 0) > 50 && "text-ember")}>{pct(full.attackSuccess)}</td>
            </tr>
            <tr>
              <th scope="row" className="py-2.5 font-normal">Task completion</th>
              <td className="py-2.5 font-mono">{pct(base.taskCompletion)}</td>
              <td className="py-2.5 font-mono">{pct(full.taskCompletion)}</td>
            </tr>
          </tbody>
        </table>
        <p className="mb-0 mt-4 text-[15px] text-body">
          Sandcage cut the attack success rate from <strong className="text-ember">{pct(base.attackSuccess)}</strong> to <strong>{pct(full.attackSuccess)}</strong>, with task completion at{" "}
          <strong>{pct(full.taskCompletion)}</strong>.
        </p>
      </Card>
      <Card as="section" aria-labelledby="bench-cats">
        <h2 id="bench-cats" className="m-0 mb-3 text-lg font-semibold text-head">
          By category
        </h2>
        <div className="sc-scroll overflow-x-auto">
          <table className="w-full min-w-[420px] border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-line text-muted">
                <th scope="col" className="py-2 font-semibold">Category</th>
                <th scope="col" className="py-2 font-semibold">baseline (attack / task)</th>
                <th scope="col" className="py-2 font-semibold">full (attack / task)</th>
              </tr>
            </thead>
            <tbody>
              {categories.map((c) => {
                const b = modeRates(results, "baseline", c);
                const f = modeRates(results, "full", c);
                return (
                  <tr key={c} className="border-b border-line-soft last:border-b-0">
                    <th scope="row" className="py-2.5 font-normal">{c.replace(/_/g, " ")}</th>
                    <td className="py-2.5 font-mono">{pct(b.attackSuccess)} / {pct(b.taskCompletion)}</td>
                    <td className="py-2.5 font-mono">{pct(f.attackSuccess)} / {pct(f.taskCompletion)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
