"use client";

import { useEffect, useState } from "react";
import { clock, countdownAnnouncement, secondsLeft } from "@/lib/derive";

/**
 * The time left to answer. The visible text ticks every second; screen readers hear it
 * only at thresholds (60, 30, 10, 5 seconds and zero), so it never chatters.
 */
export function Countdown({ deadlineMs, timeoutSeconds, label, timedOutText }: { deadlineMs: number; timeoutSeconds: number; label: string; timedOutText: string }) {
  const [now, setNow] = useState(() => Date.now());
  const [announced, setAnnounced] = useState("");
  useEffect(() => {
    const timer = setInterval(() => {
      const at = Date.now();
      setNow(at);
      const spoken = countdownAnnouncement(secondsLeft(deadlineMs, at, timeoutSeconds));
      if (spoken !== null) setAnnounced(spoken);
    }, 1000);
    return () => clearInterval(timer);
  }, [deadlineMs, timeoutSeconds]);
  const left = secondsLeft(deadlineMs, now, timeoutSeconds);

  return (
    <>
      <span data-testid="countdown" aria-hidden="true" className={`font-mono text-xs tabular-nums ${left === 0 ? "text-ember" : "text-muted"}`}>
        {left === 0 ? timedOutText : `${label} ${clock(left)}`}
      </span>
      <span role="status" className="sr-only">
        {announced}
      </span>
    </>
  );
}
