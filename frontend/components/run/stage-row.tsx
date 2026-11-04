import type { Stage } from "@/lib/derive";
import { cx } from "../ui";

const STATE_CLASS: Record<Stage["state"], string> = {
  done: "border-line-strong text-body",
  current: "border-ember font-semibold text-ember",
  waiting: "border-ember font-semibold text-ember",
  failed: "border-ember text-ember",
  upcoming: "border-dashed border-line text-faint",
};

export function StageRow({ stages }: { stages: Stage[] }) {
  return (
    <ol aria-label="Stages" className="m-0 flex list-none flex-wrap items-center gap-2 p-0 text-[13px] max-[960px]:order-3 max-[960px]:w-full">
      {stages.map((s) => (
        <li
          key={s.key}
          aria-current={s.state === "current" || s.state === "waiting" ? "step" : undefined}
          className={cx("rounded-full border px-3 py-0.5 leading-5", STATE_CLASS[s.state])}
        >
          {s.state === "done" && <span aria-hidden="true">✓ </span>}
          {s.label}
          {s.detail ? ` · ${s.detail}` : ""}
          <span className="sr-only">{` (${s.state === "upcoming" ? "not yet" : s.state})`}</span>
        </li>
      ))}
    </ol>
  );
}
