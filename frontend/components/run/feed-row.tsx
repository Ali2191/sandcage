import type { StepEvent } from "@contract";
import { COPY } from "@/lib/copy";
import { blockedLine } from "@/lib/derive";
import { actionLine, allowedSentence, ruleLine, timeLabel } from "@/lib/rows";
import { BlockedIcon, cx } from "../ui";

/**
 * One row of the live feed. A blocked row leads with Sandcage's own sentence (never the
 * agent's words), then the raw action, then the rule and where the attack was traced to.
 */
export function FeedRow({ step, flagged, flash }: { step: StepEvent; flagged: boolean; flash: boolean }) {
  if (step.blocked) {
    return (
      <li data-blocked="true" className={cx("grid grid-cols-[72px_1fr] gap-x-4 border-b border-ember-line bg-ember-glow px-8 py-4 max-[960px]:px-4", flash && "animate-flash")}>
        <time className="pt-0.5 font-mono text-xs text-ember-soft">{timeLabel(step.ts)}</time>
        <div className="min-w-0">
          <div className="flex items-start gap-2 font-semibold text-ember">
            <BlockedIcon className="mt-0.5 size-[18px]" />
            <span>{blockedLine(step)}</span>
          </div>
          <div className="mt-1 break-words font-mono text-[13px] text-detail">{actionLine(step)}</div>
          <div className="mt-0.5 break-words font-mono text-[13px] text-ember-soft">{ruleLine(step)}</div>
        </div>
      </li>
    );
  }
  return (
    <li className="grid grid-cols-[72px_1fr] gap-x-4 border-b border-line-soft px-8 py-4 max-[960px]:px-4">
      <time className="pt-0.5 font-mono text-xs text-faint">{timeLabel(step.ts)}</time>
      <div className="min-w-0">
        <div className="text-body">
          {allowedSentence(step)}
          {flagged && <span className="ml-2 rounded bg-ember-glow px-1.5 py-0.5 text-xs text-ember-soft">{COPY.live.flagged}</span>}
        </div>
        {step.tool === "run_command" && <div className="mt-0.5 break-words font-mono text-[13px] text-detail">{actionLine(step)}</div>}
      </div>
    </li>
  );
}
