"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  getBenchResults,
  getCatalogue,
  getDemoStatus,
  isRealBench,
  startRun,
  type BenchResults,
  type DemoStatus,
} from "@/lib/api";
import { COPY } from "@/lib/copy";
import { isTypingTarget } from "@/lib/keys";
import { benchTotals, resultFor } from "@/lib/bench";
import { Body, Main, Side } from "../shell";
import { TopBarSlot } from "../top-bar-slot";
import { Button, Card, ErrorCard, Kbd, Meter, Pill, Skeleton, cx } from "../ui";
import { useLoad } from "../use-load";

const NO_SCENARIOS: never[] = [];

export function Gallery() {
  const router = useRouter();
  const { state: catalogue, reload } = useLoad(getCatalogue, COPY.gallery.loadFailed);
  const [results, setResults] = useState<BenchResults | null>(null);
  const [demo, setDemo] = useState<DemoStatus | null>(null);
  const [category, setCategory] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [starting, setStarting] = useState<string | null>(null);
  const [failure, setFailure] = useState<{ id: string; message: string; needsCode: boolean } | null>(null);
  const search = useRef<HTMLInputElement>(null);

  useEffect(() => {
    getBenchResults().then(setResults, () => setResults(null)); // results are optional: errors are ignored
    getDemoStatus().then(setDemo, () => setDemo(null));
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "/" && !isTypingTarget(event.target)) {
        event.preventDefault();
        search.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const real = isRealBench(results);
  const all = catalogue.kind === "ready" ? catalogue.data.scenarios : NO_SCENARIOS;
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return all.filter((s) => (category === "all" || s.category === category) && (q === "" || `${s.title} ${s.blurb} ${s.category_label}`.toLowerCase().includes(q)));
  }, [all, category, query]);

  const run = async (id: string): Promise<void> => {
    setStarting(id);
    setFailure(null);
    try {
      const { id: runId } = await startRun({ scenario: id, guard: true, approve_plan: false });
      router.push(`/runs/${runId}`);
    } catch (cause) {
      setStarting(null);
      const e = cause instanceof ApiError ? cause : null;
      setFailure({ id, message: e && !e.needsCode ? e.message : COPY.gallery.needsCode, needsCode: e?.needsCode ?? false });
    }
  };

  const totals = real && catalogue.kind === "ready" ? benchTotals(results, catalogue.data.total) : null;
  // Attacks that worked are counted over the attack scenarios; ordinary tasks have nothing to contain.
  const attackTotal = all.filter((s) => s.benign !== true).length || all.length;

  return (
    <Body>
      <Main flush>
        <TopBarSlot>
          <div className="relative w-[260px] max-w-full max-[960px]:ml-auto">
            <input
              ref={search}
              type="search"
              aria-label={COPY.gallery.search}
              placeholder={COPY.gallery.search}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="h-9 w-full rounded-full border border-line bg-sunken pl-4 pr-10 text-sm text-fg placeholder:text-faint"
            />
            <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2">
              <Kbd>/</Kbd>
            </span>
          </div>
        </TopBarSlot>
        <div className="px-10 pb-6 pt-10 max-[960px]:px-4 max-[960px]:pt-7">
          <h1 className="m-0 text-[40px] font-bold tracking-tight text-head max-[960px]:text-[28px]">{COPY.gallery.headline}</h1>
          <p className="mb-0 mt-3 max-w-[62ch] text-[17px] text-muted">{COPY.gallery.sub}</p>
        </div>

        {catalogue.kind === "ready" && (
          <div className="sticky top-0 z-10 flex flex-wrap items-center gap-2 border-y border-line-soft bg-bar px-10 py-3.5 max-[960px]:top-14 max-[960px]:px-4">
            <Chip pressed={category === "all"} onClick={() => setCategory("all")} label="All" count={catalogue.data.total} />
            {catalogue.data.categories.map((c) => (
              <Chip key={c.key} pressed={category === c.key} onClick={() => setCategory(c.key)} label={c.label} count={c.count} />
            ))}
            <span className="ml-auto text-xs text-faint" aria-live="polite">
              Showing {shown.length} of {catalogue.data.total}
            </span>
          </div>
        )}

        <div className="px-10 py-6 max-[960px]:px-4">
          {catalogue.kind === "loading" && (
            <div role="status" aria-label="Loading attacks" className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(250px,1fr))]">
              {[0, 1, 2, 3, 4, 5].map((i) => (
                <Skeleton key={i} className="h-44" />
              ))}
            </div>
          )}
          {catalogue.kind === "error" && <ErrorCard onRetry={reload}>{catalogue.message}</ErrorCard>}
          {catalogue.kind === "ready" && shown.length === 0 && <p className="text-muted">{COPY.gallery.noMatch}</p>}
          {catalogue.kind === "ready" && shown.length > 0 && (
            <ul className="m-0 grid list-none gap-4 p-0 [grid-template-columns:repeat(auto-fit,minmax(250px,1fr))]">
              {shown.map((s) => {
                const result = real ? resultFor(results, s.id) : null;
                return (
                  <li key={s.id}>
                    <Card className="flex h-full flex-col gap-2.5 transition-colors hover:border-line-strong hover:bg-raised">
                      <Pill>{s.category_label}</Pill>
                      <h2 className="m-0 text-base font-semibold text-head">{s.title}</h2>
                      <p className="m-0 text-sm text-muted">{s.blurb}</p>
                      {result && (
                        <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                          <dt className="text-muted">Watching</dt>
                          <dd className={cx("m-0", result.baseline.attack_succeeded ? "font-semibold text-ember" : "text-body")}>{result.baseline.attack_succeeded ? "attack got in" : "safe"}</dd>
                          <dt className="text-muted">Sandcage</dt>
                          <dd className="m-0 text-body">{result.full.attack_succeeded ? "attack got in" : "blocked"}</dd>
                        </dl>
                      )}
                      <div className="mt-auto pt-2">
                        <Button size="sm" busy={starting === s.id} disabled={demo?.paused === true} onClick={() => void run(s.id)} aria-label={`${COPY.gallery.run}: ${s.title}`}>
                          {COPY.gallery.run}
                        </Button>
                        {failure?.id === s.id && (
                          <p role="alert" className="mb-0 mt-2 text-sm text-ember">
                            {failure.message}
                            {failure.needsCode && (
                              <>
                                {" "}
                                <Link href="/#access-code" className="text-ember">
                                  {COPY.gallery.enterIt}
                                </Link>
                              </>
                            )}
                          </p>
                        )}
                      </div>
                    </Card>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </Main>
      <Side label="About the attacks">
        <Card as="section">
          <h2 className="m-0 mb-3 text-base font-semibold text-head">Across all {catalogue.kind === "ready" ? catalogue.data.total : "…"} attacks</h2>
          {totals && results ? (
            <div className="space-y-3">
              <p className="m-0 text-sm text-muted">Model: {results.model}</p>
              <Meter label="Attacks that worked, watching only" value={`${totals.baselineAttacks} / ${attackTotal}`} fraction={totals.baselineAttacks / attackTotal} tone="ember" />
              <Meter label="Attacks that worked, with Sandcage" value={`${totals.fullAttacks} / ${attackTotal}`} fraction={totals.fullAttacks / attackTotal} />
              <Meter label="Real tasks still finished" value={`${totals.tasksFinished} / ${totals.total}`} fraction={totals.tasksFinished / totals.total} />
            </div>
          ) : results ? (
            <p className="m-0 text-sm text-muted">{COPY.gallery.scriptedOnly}</p>
          ) : (
            <p className="m-0 text-sm text-muted">{COPY.gallery.none}</p>
          )}
        </Card>
        <Card as="section">
          <h2 className="m-0 mb-2 text-base font-semibold text-head">{COPY.gallery.why.title}</h2>
          <p className="m-0 text-sm text-muted">{COPY.gallery.why.body}</p>
        </Card>
      </Side>
    </Body>
  );
}

function Chip({ pressed, onClick, label, count }: { pressed: boolean; onClick: () => void; label: string; count: number }) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className={cx("cursor-pointer rounded-full border px-3.5 py-1 text-sm", pressed ? "border-fg bg-fg font-semibold text-night" : "border-line text-muted hover:text-fg")}
    >
      {label} <span className={pressed ? "text-night/70" : "text-faint"}>{count}</span>
    </button>
  );
}
