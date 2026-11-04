"use client";

import type { Budget, LearnSuggestion, Plan, Trifecta } from "@contract";
import { COPY } from "@/lib/copy";
import { clock, measuredTokens, type Checkpoints, type LegState, type Reach, type TrifectaLegs } from "@/lib/derive";
import { Button, Card, Meter, Pill, cx } from "../ui";

function CardTitle({ children, right }: { children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="mb-3 flex items-baseline justify-between gap-3">
      <h3 className="m-0 text-base font-semibold text-head">{children}</h3>
      {right}
    </div>
  );
}

export function BudgetCard({ budget }: { budget: Budget }) {
  const tokens = measuredTokens(budget);
  return (
    <Card as="section">
      <CardTitle>{COPY.live.budget}</CardTitle>
      <div className="space-y-3">
        <Meter label="Steps" value={`${budget.steps} / ${budget.max_steps}`} fraction={budget.steps / budget.max_steps} />
        {tokens !== null && <Meter label="Tokens" value={`${tokens} / ${budget.max_tokens}`} fraction={tokens / budget.max_tokens} />}
        <Meter label="Time" value={`${clock(budget.seconds)} / ${clock(budget.max_seconds)}`} fraction={null} />
      </div>
    </Card>
  );
}

function list(values: readonly string[], none: string): string {
  return values.length > 0 ? values.join(", ") : none;
}

export function PlanHeldCard({ plan, guard, sandbox }: { plan: Plan; guard: boolean; sandbox: string | null }) {
  const rows: { label: string; value: string; mono: boolean }[] = [
    { label: "May edit", value: list(plan.manifest.write_paths, "nothing"), mono: true },
    { label: "May run", value: list(plan.manifest.commands, "nothing"), mono: true },
    { label: "Network", value: list(plan.manifest.network, "none"), mono: true },
  ];
  if (guard) rows.push({ label: "Secrets", value: COPY.live.secretsNone, mono: true });
  if (sandbox) rows.push({ label: "Sandbox", value: sandbox, mono: true });
  return (
    <Card as="section">
      <CardTitle>{COPY.live.planHeld}</CardTitle>
      <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
        {rows.map((r) => (
          <div key={r.label} className="contents">
            <dt className="text-muted">{r.label}</dt>
            <dd className={cx("m-0 min-w-0 break-words text-fg", r.mono && "font-mono")}>{r.value}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

export function UntrustedCard({ paths }: { paths: readonly string[] }) {
  return (
    <Card as="section">
      <CardTitle>{COPY.live.untrusted}</CardTitle>
      <ul className="m-0 list-none space-y-2 p-0 text-sm">
        {paths.map((p) => (
          <li key={p} className="flex items-baseline justify-between gap-3">
            <span className="min-w-0 break-all font-mono text-fg">{p}</span>
            <span className="shrink-0 text-ember">{COPY.live.hasInstructions}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export function CheckpointsCard({
  checkpoints,
  onRewind,
  rewinding,
}: {
  checkpoints: Checkpoints;
  /** Present only while the agent is acting: the Rewind buttons show. */
  onRewind?: (id: number) => void;
  rewinding?: number | null;
}) {
  const saved = checkpoints.rows.length;
  const restore = checkpoints.lastRestore;
  return (
    <Card as="section">
      <CardTitle right={<span className="text-xs text-muted">{saved} saved</span>}>{COPY.live.checkpoints}</CardTitle>
      <ul className="m-0 list-none space-y-2.5 p-0">
        {checkpoints.rows.map((row) => (
          <li key={row.id} className="flex items-center gap-3 text-sm">
            <Pill tone="ember-soft" mono>
              {row.id}
            </Pill>
            <span className="min-w-0 flex-1 text-body">{row.id === 0 ? COPY.live.original : row.step !== null ? `after step ${row.step}` : "saved"}</span>
            {onRewind && (
              <Button size="sm" busy={rewinding === row.id} onClick={() => onRewind(row.id)} aria-label={`Rewind to checkpoint ${row.id}`}>
                {COPY.live.rewind}
              </Button>
            )}
          </li>
        ))}
      </ul>
      {restore && <p className="mb-0 mt-3 text-sm text-muted">{`Restored checkpoint ${restore.id} by ${restore.by === "human" ? "you" : "the agent"}.`}</p>}
    </Card>
  );
}

const LEG_LABEL: Record<LegState, string> = { present: "present", blocked: "blocked", absent: "not present" };

export function TrifectaCard({ legs }: { legs: TrifectaLegs }) {
  const rows: [string, LegState][] = [
    [COPY.report.trifecta.legs[0], legs.privateData],
    [COPY.report.trifecta.legs[1], legs.untrustedContent],
    [COPY.report.trifecta.legs[2], legs.wayOut],
  ];
  return (
    <Card as="section">
      <CardTitle>{COPY.report.trifecta.title}</CardTitle>
      <p className="mb-3 mt-0 text-sm text-muted">{legs.allAtOnce ? COPY.report.trifecta.had : COPY.report.trifecta.never}</p>
      <ul className="m-0 list-none space-y-2.5 p-0">
        {rows.map(([label, state]) => (
          <li key={label} className="flex items-center justify-between gap-3 text-[15px]">
            <span>{label}</span>
            <Pill tone={state === "present" ? "ember" : "outline"}>{LEG_LABEL[state]}</Pill>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export function ReachedCard({ tried, changed }: { tried: readonly Reach[]; changed: readonly string[] }) {
  return (
    <Card as="section">
      <CardTitle>{COPY.report.reached.title}</CardTitle>
      <div className="grid grid-cols-2 gap-x-4 text-sm">
        <div className="min-w-0">
          <div className="mb-2 text-muted">{COPY.report.reached.tried}</div>
          <ul className="m-0 list-none space-y-1.5 p-0">
            {tried.map((t) => (
              <li key={`${t.kind}:${t.target}`} className={cx("break-words font-mono", t.blocked ? "text-ember line-through" : "text-fg")}>
                {t.target}
                {t.blocked && <span className="sr-only"> (blocked)</span>}
              </li>
            ))}
          </ul>
        </div>
        <div className="min-w-0">
          <div className="mb-2 text-muted">{COPY.report.reached.changed}</div>
          {changed.length === 0 ? (
            <span className="text-faint">{COPY.report.reached.nothing}</span>
          ) : (
            <ul className="m-0 list-none space-y-1.5 p-0">
              {changed.map((f) => (
                <li key={f} className="break-words font-mono text-fg">
                  {f}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Card>
  );
}

export type LearnStatus = "added" | "already" | "dismissed" | "busy" | "error";

export function LearnCard({
  suggestions,
  status,
  onAdd,
  onDismiss,
}: {
  suggestions: readonly LearnSuggestion[];
  status: Readonly<Record<string, LearnStatus | undefined>>;
  onAdd: (id: string) => void;
  onDismiss: (id: string) => void;
}) {
  const visible = suggestions.filter((s) => status[s.id] !== "dismissed");
  if (visible.length === 0) return null;
  return (
    <Card as="section" dashed>
      <CardTitle>{COPY.report.learned.title}</CardTitle>
      <ul className="m-0 list-none space-y-4 p-0">
        {visible.map((s) => {
          const st = status[s.id];
          return (
            <li key={s.id} className="text-sm">
              <p className="m-0 mb-2 text-body">{s.explanation}</p>
              {st === "added" ? (
                <span className="text-ember-soft">{COPY.report.learned.added}</span>
              ) : st === "already" ? (
                <span className="text-muted">{COPY.report.learned.already}</span>
              ) : (
                <div className="flex items-center gap-2">
                  <Button size="sm" busy={st === "busy"} onClick={() => onAdd(s.id)}>
                    {COPY.report.learned.add}
                  </Button>
                  <Button size="sm" kind="ghost" onClick={() => onDismiss(s.id)}>
                    {COPY.report.learned.dismiss}
                  </Button>
                  {st === "error" && <span role="alert" className="text-ember">The rule could not be added.</span>}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

export type { Trifecta };
