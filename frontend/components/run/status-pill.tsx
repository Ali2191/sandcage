import { COPY } from "@/lib/copy";
import { cx } from "../ui";

export type ConnectionView = "live" | "finished" | "disconnected";

/** The top-bar status: needs you (pulsing ember), disconnected, or live (the one green dot). */
export function StatusPill({ connection, waiting }: { connection: ConnectionView; waiting: boolean }) {
  const base = "inline-flex items-center gap-2 whitespace-nowrap rounded-full border px-3 py-1 text-xs";
  if (waiting && connection !== "finished") {
    return (
      <span role="status" className={cx(base, "border-ember-line bg-ember-glow text-ember-soft")}>
        <span aria-hidden="true" className="size-2 animate-pulse-soft rounded-full bg-ember" />
        {COPY.status.needsYou}
      </span>
    );
  }
  if (connection === "disconnected") {
    return (
      <span role="status" className={cx(base, "border-line-strong text-muted")}>
        {COPY.status.disconnected}
      </span>
    );
  }
  return (
    <span role="status" className={cx(base, "border-line text-muted")}>
      <span aria-hidden="true" className={cx("size-2 rounded-full bg-live", connection === "live" && "animate-pulse-soft")} />
      {connection === "finished" ? COPY.status.finished : COPY.status.live}
    </span>
  );
}
