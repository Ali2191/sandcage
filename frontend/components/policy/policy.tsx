"use client";

import { getPolicy, type PolicyRule } from "@/lib/api";
import { COPY } from "@/lib/copy";
import { Card, ErrorCard, Pill, Skeleton } from "../ui";
import { useLoad } from "../use-load";

function RuleRow({ rule }: { rule: PolicyRule }) {
  return (
    <li className="border-b border-line-soft py-3.5 last:border-b-0">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="break-all font-mono text-sm text-ember-soft">{rule.id}</span>
        {rule.risk && <Pill>{rule.risk}</Pill>}
        <span className="font-mono text-xs text-faint">{rule.tools.join(", ")}</span>
      </div>
      <p className="m-0 mt-1 text-[15px] text-body">{rule.reason}</p>
    </li>
  );
}

export function Policy() {
  const { state, reload } = useLoad(getPolicy, "Could not load the rulebook.");

  return (
    <>
      <h1 className="m-0 text-[34px] font-bold tracking-tight text-head">{COPY.policy.headline}</h1>
      <p className="mb-6 mt-3 max-w-[62ch] text-[17px] text-muted">{COPY.policy.sub}</p>
      {state.kind === "loading" && <Skeleton label="Loading the rulebook" className="h-96" />}
      {state.kind === "error" && <ErrorCard onRetry={reload}>{state.message}</ErrorCard>}
      {state.kind === "ready" && (
        <div className="space-y-8">
          <div className="flex flex-wrap gap-2" aria-label="Rule counts">
            <Pill tone="ember-soft">{state.data.counts.deny} always blocked</Pill>
            <Pill>{state.data.counts.ask} ask first</Pill>
            <Pill>{state.data.counts.allow} allowed</Pill>
          </div>
          {(["deny", "ask", "allow"] as const).map((group) => {
            const rules = state.data.rules.filter((r) => r.decision === group);
            if (rules.length === 0) return null;
            return (
              <section key={group} aria-labelledby={`g-${group}`}>
                <h2 id={`g-${group}`} className="m-0 text-base font-semibold text-head">{COPY.policy.groups[group].title}</h2>
                <p className="mb-3 mt-1 text-sm text-muted">{COPY.policy.groups[group].blurb}</p>
                <Card className="px-5 py-1">
                  <ul className="m-0 list-none p-0">
                    {rules.map((r) => (
                      <RuleRow key={r.id} rule={r} />
                    ))}
                  </ul>
                </Card>
              </section>
            );
          })}
          <section aria-labelledby="g-secrets">
            <h2 id="g-secrets" className="m-0 text-base font-semibold text-head">{COPY.policy.secrets.title}</h2>
            <p className="mb-3 mt-1 text-sm text-muted">{COPY.policy.secrets.body}</p>
            <ul className="m-0 flex list-none flex-wrap gap-2 p-0">
              {state.data.secret_patterns.map((p) => (
                <li key={p}>
                  <Pill tone="ember-soft" mono>{p}</Pill>
                </li>
              ))}
            </ul>
          </section>
        </div>
      )}
    </>
  );
}
