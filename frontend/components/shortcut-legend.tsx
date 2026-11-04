"use client";

import { useEffect, useRef, useState } from "react";
import { COPY } from "@/lib/copy";
import { isTypingTarget } from "@/lib/keys";
import { Button } from "./ui";

/** A visible legend of every shortcut, opened with "?" or its button in the corner. */
export function ShortcutLegend() {
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "?" && !isTypingTarget(event.target)) setOpen((o) => !o);
      if (event.key === "Escape" && open) {
        event.stopPropagation();
        setOpen(false);
        opener.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open]);

  return (
    <>
      <button
        ref={opener}
        type="button"
        aria-label={COPY.shortcuts.title}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="fixed bottom-3 right-3 z-40 flex size-8 cursor-pointer items-center justify-center rounded-full border border-line-strong bg-bar font-mono text-[13px] text-muted hover:border-fg hover:text-fg max-[960px]:bottom-3"
      >
        ?
      </button>
      {open && (
        <div role="dialog" aria-label={COPY.shortcuts.title} className="fixed bottom-14 right-3 z-40 w-[min(340px,calc(100vw-24px))] rounded-card border border-line bg-graphite p-5 shadow-2xl">
          <div className="mb-3 font-semibold text-head">{COPY.shortcuts.title}</div>
          <dl className="m-0 grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-2 text-sm">
            {COPY.shortcuts.rows.map((row) => (
              <div key={row.keys} className="contents">
                <dt>
                  <kbd>{row.keys}</kbd>
                </dt>
                <dd className="m-0 text-body">{row.text}</dd>
              </div>
            ))}
          </dl>
          <Button size="sm" className="mt-4" onClick={() => setOpen(false)}>
            Close
          </Button>
        </div>
      )}
    </>
  );
}
