"use client";

import { useEffect, useRef, useState } from "react";
import type { Approval, Choice } from "@contract";
import { COPY } from "@/lib/copy";
import { choiceForKey, isTypingTarget } from "@/lib/keys";
import { Button, Eyebrow, Facts, Kbd, cx } from "./ui";
import { Countdown } from "./countdown";
import type { ApprovalState } from "./use-approval";

/** "auto-deny", "auto-end", "auto-keep": the first word of the choice a timeout picks. */
export function autoLabel(approval: Approval): string {
  const picked = approval.choices.find((c) => c.id === approval.on_timeout);
  const word = picked?.label.split(/\s+/)[0]?.toLowerCase() ?? "deny";
  return `auto-${word} in`;
}

function Body({ blocks }: { blocks: Approval["body"] }) {
  return (
    <div className="mt-2 space-y-2">
      {blocks.map((block, index) => {
        switch (block.type) {
          case "code":
            return (
              <pre key={index} className="m-0 overflow-x-auto whitespace-pre-wrap break-words font-mono text-sm text-detail">
                {block.text}
              </pre>
            );
          case "text":
            return (
              <p key={index} className={cx("m-0 text-[15px]", block.muted ? "text-sm text-muted" : "text-body")}>
                {block.text}
              </p>
            );
          case "facts":
            return <Facts key={index} rows={block.rows.map((r) => ({ label: r.label, value: r.value, mono: r.mono }))} />;
          case "findings":
            return (
              <ul key={index} className="m-0 list-disc space-y-1.5 pl-5 text-[15px] text-body">
                {block.items.map((item, i) => (
                  <li key={i}>
                    <span className="font-mono text-sm text-fg">{item.location}</span> {item.text}
                  </li>
                ))}
              </ul>
            );
        }
      })}
    </div>
  );
}

function ChoiceButton({ choice, state, onChoose, disabled }: { choice: Choice; state: ApprovalState; onChoose: (c: Choice) => void; disabled: boolean }) {
  return (
    <Button
      kind={choice.primary ? "main" : "secondary"}
      size="lg"
      className="w-full"
      busy={state.sending === choice.id}
      disabled={disabled}
      onClick={() => onChoose(choice)}
      aria-keyshortcuts={choice.shortcut ?? undefined}
    >
      {choice.label}
      {choice.shortcut !== null && <Kbd>{choice.shortcut}</Kbd>}
    </Button>
  );
}

/**
 * One banner for every approval kind, rendered from the approval record alone: the
 * wording, buttons, shortcut keys, optional free-text box and timeout all come from it.
 */
export function DecisionBanner({
  approval,
  deadlineMs,
  state,
  onDecide,
}: {
  approval: Approval;
  deadlineMs: number;
  state: ApprovalState;
  onDecide: (choiceId: string, answer?: string) => void;
}) {
  const [text, setText] = useState("");
  const boxRef = useRef<HTMLTextAreaElement>(null);
  const locked = state.sending !== null || state.sent;
  const free = approval.free_text;
  const needsText = (choice: Choice): boolean => free !== null && choice.approves && free.required && text.trim() === "";

  const choose = (choice: Choice): void => {
    if (locked || needsText(choice)) return;
    onDecide(choice.id, free !== null && choice.approves ? text.trim() : undefined);
  };

  // Shortcut keys come from the record. They never fire while typing or after an answer.
  useEffect(() => {
    if (locked) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.metaKey || event.ctrlKey || event.altKey || isTypingTarget(event.target)) return;
      const id = choiceForKey(event.key, approval.choices);
      const choice = approval.choices.find((c) => c.id === id);
      if (choice) {
        event.preventDefault();
        choose(choice);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [approval, locked, text]);

  // Typing is the action for a question, so it takes focus when it appears.
  useEffect(() => {
    if (free !== null) boxRef.current?.focus();
  }, [free, approval.id]);

  const status = state.sent ? (
    <span className="text-xs text-muted">{COPY.live.sent}</span>
  ) : (
    <Countdown deadlineMs={deadlineMs} timeoutSeconds={approval.timeout_seconds} label={autoLabel(approval)} timedOutText={approval.timeout_text} />
  );
  const problem = state.error ? <span role="alert" className="text-xs text-ember">{state.error}</span> : null;

  const shell = "rounded-card border border-ember-border bg-[linear-gradient(135deg,rgb(245_165_74/0.09),rgb(245_165_74/0.03))] p-6 max-[960px]:p-4";

  if (approval.layout === "question" && free !== null) {
    return (
      <section role="alert" aria-label={approval.eyebrow} data-approval-kind={approval.kind} className={shell}>
        <Eyebrow ember>{approval.eyebrow}</Eyebrow>
        <label htmlFor="approval-text" className="mt-1.5 block text-lg font-semibold leading-snug text-head">
          {approval.headline}
        </label>
        <textarea
          id="approval-text"
          ref={boxRef}
          rows={2}
          maxLength={free.max_length}
          value={text}
          disabled={locked}
          placeholder={free.placeholder}
          onChange={(e) => setText(e.target.value)}
          className="mt-3 block w-full resize-y rounded-[14px] border border-line-strong bg-sunken px-4 py-3 text-[15px] text-fg placeholder:text-faint"
        />
        <div className="mt-3 flex flex-wrap items-center gap-3">
          {approval.choices.map((choice) => (
            <Button
              key={choice.id}
              kind={choice.primary ? "main" : "secondary"}
              busy={state.sending === choice.id}
              disabled={locked || needsText(choice)}
              onClick={() => choose(choice)}
            >
              {choice.label}
            </Button>
          ))}
          {status}
          {problem}
        </div>
        {approval.note && <p className="mb-0 mt-3 text-xs text-muted">{approval.note}</p>}
      </section>
    );
  }

  return (
    <section role="alert" aria-label={approval.eyebrow} data-approval-kind={approval.kind} className={cx(shell, "grid gap-6 md:grid-cols-[minmax(0,1fr)_240px]")}>
      <div className="min-w-0">
        <Eyebrow ember>{approval.eyebrow}</Eyebrow>
        <h2 className="m-0 mt-1.5 text-[22px] font-semibold leading-tight text-head max-[960px]:text-lg">{approval.headline}</h2>
        <Body blocks={approval.body} />
      </div>
      <div className="flex flex-col gap-2.5">
        {approval.choices.map((choice) => (
          <ChoiceButton key={choice.id} choice={choice} state={state} onChoose={choose} disabled={locked} />
        ))}
        {approval.note && <p className="m-0 text-center text-xs text-muted">{approval.note}</p>}
        <div className="flex flex-col items-center gap-1 text-center">
          {status}
          {problem}
        </div>
      </div>
    </section>
  );
}
