"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ApiError, getDemoStatus, listRepos, startRun, unlock, type DemoStatus } from "@/lib/api";
import { accessCode, rememberAccessCode } from "@/lib/access";
import { COPY } from "@/lib/copy";
import { Button, Card, ErrorCard, Eyebrow, Kbd, Pill, Skeleton, cx } from "../ui";
import { useLoad } from "../use-load";

const noSubscribe = (): (() => void) => () => undefined;
type Choice = { kind: "repo"; name: string } | { kind: "scripted" };

function RadioDot({ on }: { on: boolean }) {
  return (
    <span aria-hidden="true" className={cx("inline-flex size-3.5 shrink-0 items-center justify-center rounded-full border", on ? "border-fg" : "border-faint")}>
      {on && <span className="size-1.5 rounded-full bg-fg" />}
    </span>
  );
}

function RepoCard({ selected, onSelect, title, meta, pill, children, name }: { selected: boolean; onSelect: () => void; title: string; meta: string; pill: React.ReactNode; children?: React.ReactNode; name: string }) {
  return (
    <label
      className={cx(
        "flex min-w-0 cursor-pointer flex-col gap-1.5 rounded-card border p-4 transition-colors has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-fg",
        selected ? "border-[1.5px] border-fg bg-raised" : "border-line bg-graphite hover:border-line-strong",
      )}
    >
      <input type="radio" name="repository" value={name} checked={selected} onChange={onSelect} className="sr-only" />
      <span className="flex items-center gap-2.5 font-semibold text-head">
        <RadioDot on={selected} />
        {title}
      </span>
      <span className="text-sm text-muted">{meta}</span>
      {pill}
      {children}
    </label>
  );
}

export function StartForm() {
  const router = useRouter();
  const ids = { task: useId(), code: useId(), scenario: useId() };
  const { state: loadedRepos, reload: reloadRepos } = useLoad(listRepos, COPY.start.reposFailed);
  const repos = loadedRepos.kind === "ready" ? ({ kind: "ready", names: loadedRepos.data } as const) : loadedRepos;
  const [demo, setDemo] = useState<DemoStatus | null>(null);
  const [picked, setPicked] = useState<Choice | null>(null);
  const storedCode = useSyncExternalStore(noSubscribe, () => accessCode() ?? "", () => "");
  // Until the person picks, the first repo the server lists is selected.
  const defaultRepo = repos.kind === "ready" ? (repos.names[0] ?? COPY.start.fallbackRepo) : null;
  const choice = useMemo<Choice | null>(() => picked ?? (defaultRepo !== null ? { kind: "repo", name: defaultRepo } : null), [picked, defaultRepo]);
  const [scenario, setScenario] = useState<string>(COPY.start.scenarios[0]?.id ?? "demo");
  const [task, setTask] = useState<string>(COPY.start.taskDefault);
  const [guard, setGuard] = useState(true);
  const [approvePlan, setApprovePlan] = useState(false);
  const [typedCode, setTypedCode] = useState<string | null>(null);
  const code = typedCode ?? storedCode;
  const [codeError, setCodeError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<{ message: string; running: string | null } | null>(null);
  const formRef = useRef<HTMLFormElement>(null);

  useEffect(() => {
    getDemoStatus().then(setDemo, () => setDemo(null)); // can't load: behave as an open local panel
  }, []);

  const needsCode = demo?.access_required === true;
  const paused = demo?.paused === true;
  const tooLong = task.length > COPY.start.taskMax;
  const canStart = choice !== null && task.trim() !== "" && !tooLong && !starting && !paused && (!needsCode || code.trim() !== "");

  const submit = useCallback(async (): Promise<void> => {
    if (!canStart || choice === null) return;
    setStarting(true);
    setStartError(null);
    setCodeError(null);
    try {
      if (needsCode) {
        rememberAccessCode(code.trim());
        await unlock();
      }
      const body = choice.kind === "scripted" ? { repo: COPY.start.fallbackRepo, fake_scenario: scenario, task: task.trim(), guard, approve_plan: approvePlan } : { repo: choice.name, task: task.trim(), guard, approve_plan: approvePlan };
      const { id } = await startRun(body);
      router.push(`/runs/${id}`);
    } catch (cause) {
      setStarting(false);
      if (cause instanceof ApiError && (cause.needsCode || cause.status === 429)) {
        setCodeError(cause.message);
      } else if (cause instanceof ApiError) {
        const running = /\b([0-9a-f]{12})\b/.exec(cause.message)?.[1] ?? null;
        setStartError({ message: cause.message, running: cause.status === 409 ? running : null });
      } else setStartError({ message: COPY.live.sendFailed, running: null });
    }
  }, [canStart, choice, needsCode, code, scenario, task, guard, approvePlan, router]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        void submit();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [submit]);

  const repoName = choice?.kind === "repo" ? choice.name : COPY.start.chips.scripted;

  return (
    <form
      ref={formRef}
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      className="flex min-h-0 flex-col"
    >
      <div className="mx-auto w-full max-w-[920px] px-10 pb-8 pt-10 max-[960px]:px-4 max-[960px]:pt-7">
        <h1 className="m-0 max-w-[24ch] text-[40px] font-bold leading-[1.12] tracking-tight text-head max-[960px]:text-[28px]">{COPY.start.headline}</h1>
        <p className="mb-9 mt-4 max-w-[58ch] text-[17px] text-muted">{COPY.start.sub}</p>

        <Eyebrow className="mb-3">{COPY.start.repoEyebrow}</Eyebrow>
        {repos.kind === "loading" && (
          <div role="status" aria-label="Loading repositories" className="grid gap-3 md:grid-cols-3">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-28" />
            ))}
          </div>
        )}
        {repos.kind === "error" && <ErrorCard onRetry={reloadRepos}>{repos.message}</ErrorCard>}
        {repos.kind === "ready" && (
          <fieldset className="m-0 grid gap-3 border-0 p-0 md:grid-cols-3">
            <legend className="sr-only">Repository</legend>
            {(repos.names.length > 0 ? repos.names : [COPY.start.fallbackRepo]).slice(0, 1).map((name) => (
              <RepoCard
                key={name}
                name={name}
                selected={choice?.kind === "repo" && choice.name === name}
                onSelect={() => setPicked({ kind: "repo", name })}
                title={name}
                meta={COPY.start.repoMeta}
                pill={<Pill tone="ember-soft">{COPY.start.repoPill}</Pill>}
              />
            ))}
            <RepoCard
              name="scripted"
              selected={choice?.kind === "scripted"}
              onSelect={() => setPicked({ kind: "scripted" })}
              title={COPY.start.scripted.title}
              meta={COPY.start.scripted.meta}
              pill={choice?.kind === "scripted" ? null : <Pill>{COPY.start.scripted.pill}</Pill>}
            >
              {choice?.kind === "scripted" && (
                <select
                  aria-label={COPY.start.scripted.label}
                  value={scenario}
                  onChange={(e) => setScenario(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-line-strong bg-sunken px-2 py-1.5 text-sm text-fg"
                >
                  {COPY.start.scenarios.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.label}
                    </option>
                  ))}
                </select>
              )}
            </RepoCard>
            <div aria-disabled="true" className="rounded-card border border-dashed border-line p-4 opacity-60">
              <div className="font-semibold text-muted">{COPY.start.own.title}</div>
              <div className="mt-1.5 text-sm text-faint">{COPY.start.own.meta}</div>
            </div>
          </fieldset>
        )}

        <Eyebrow className="mb-3 mt-9">
          <label htmlFor={ids.task}>{COPY.start.taskEyebrow}</label>
        </Eyebrow>
        <textarea
          id={ids.task}
          rows={3}
          value={task}
          maxLength={COPY.start.taskMax + 200}
          aria-describedby={`${ids.task}-help`}
          aria-invalid={tooLong || undefined}
          onChange={(e) => setTask(e.target.value)}
          className={cx("block w-full resize-y rounded-[14px] border bg-sunken px-4 py-3 text-base text-fg hover:border-line-strong", tooLong ? "border-ember" : "border-line")}
        />
        <p id={`${ids.task}-help`} className="mb-0 mt-2 text-sm text-faint">
          {tooLong ? `The task is ${task.length - COPY.start.taskMax} characters too long.` : COPY.start.taskHelp}
        </p>

        <Eyebrow className="mb-3 mt-9">{COPY.start.safetyEyebrow}</Eyebrow>
        <div className="grid gap-3 md:grid-cols-2">
          <Card className="flex items-start gap-4">
            <button
              type="button"
              role="switch"
              aria-checked={guard}
              aria-labelledby={`${ids.task}-guard`}
              onClick={() => setGuard((g) => !g)}
              className={cx("relative h-7 w-12 shrink-0 cursor-pointer rounded-full border-0 transition-colors", guard ? "bg-white" : "bg-line-strong")}
            >
              <span className={cx("absolute top-1 size-5 rounded-full bg-night transition-all", guard ? "left-6" : "left-1")} />
            </button>
            <div>
              <div id={`${ids.task}-guard`} className="font-semibold text-head">
                {guard ? COPY.start.guardOn : COPY.start.guardOff}
              </div>
              <div className="mt-1 text-sm text-muted">{guard ? COPY.start.guardOnHelp : COPY.start.guardOffHelp}</div>
            </div>
          </Card>
          <label className="flex cursor-pointer items-start gap-4 rounded-card border border-line bg-graphite p-5">
            <input type="checkbox" checked={approvePlan} onChange={(e) => setApprovePlan(e.target.checked)} className="mt-1 size-5 accent-[#c8c8c8]" />
            <span>
              <span className="block font-semibold text-head">{COPY.start.approvePlan}</span>
              <span className="mt-1 block text-sm text-muted">{COPY.start.approvePlanHelp}</span>
            </span>
          </label>
        </div>

        {needsCode && (
          <>
            <Eyebrow className="mb-3 mt-9">
              <label htmlFor={ids.code}>{COPY.start.codeEyebrow}</label>
            </Eyebrow>
            <input
              id={ids.code}
              type="password"
              autoComplete="off"
              maxLength={200}
              value={code}
              aria-invalid={codeError !== null || undefined}
              aria-describedby={`${ids.code}-help`}
              onChange={(e) => {
                setTypedCode(e.target.value);
                setCodeError(null);
              }}
              className={cx("block w-full max-w-[360px] rounded-[14px] border bg-sunken px-4 py-2.5 text-base text-fg", codeError ? "border-ember" : "border-line hover:border-line-strong")}
            />
            <p id={`${ids.code}-help`} className={cx("mb-0 mt-2 max-w-[60ch] text-sm", codeError ? "text-ember" : "text-faint")}>
              {codeError ?? COPY.start.codeHelp}
            </p>
          </>
        )}

        {paused && (
          <Card alert className="mt-6">
            <div className="font-semibold text-ember">{COPY.start.pausedTitle}</div>
            <p className="m-0 mt-1 text-body">{demo?.message}</p>
          </Card>
        )}
        {startError && (
          <Card alert className="mt-6">
            <div role="alert">
              <div className="font-semibold text-ember">{COPY.start.errorTitle}</div>
              <p className="m-0 mt-1 text-body">{startError.message}</p>
              {startError.running && (
                <Link href={`/runs/${startError.running}`} className="mt-2 inline-block">
                  {COPY.start.openRunning}
                </Link>
              )}
            </div>
          </Card>
        )}
      </div>

      <div className="sticky bottom-0 z-10 mt-auto border-t border-line-soft bg-bar px-10 py-3.5 max-[960px]:static max-[960px]:border-t-0 max-[960px]:bg-transparent max-[960px]:px-4">
        <div className="mx-auto flex max-w-[920px] flex-wrap items-center justify-between gap-3">
          <ul className="m-0 flex list-none flex-wrap gap-2 p-0" aria-label="Your choices">
            <li><Pill mono>{repoName}</Pill></li>
            <li><Pill mono>{guard ? COPY.start.chips.guardOn : COPY.start.chips.guardOff}</Pill></li>
            <li><Pill mono>{approvePlan ? COPY.start.chips.planOn : COPY.start.chips.planOff}</Pill></li>
          </ul>
          <Button type="submit" kind="main" size="lg" busy={starting} disabled={!canStart} aria-keyshortcuts="Meta+Enter Control+Enter">
            {COPY.start.startRun}
            <Kbd>⌘ ↵</Kbd>
          </Button>
        </div>
      </div>
    </form>
  );
}
