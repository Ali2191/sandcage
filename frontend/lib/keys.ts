/** True when keyboard focus is in something people type into: shortcuts must stay quiet. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

/** The action a key triggers on a banner, given its choices' shortcut letters. */
export function choiceForKey(key: string, choices: readonly { id: string; shortcut: string | null }[]): string | null {
  if (key.length !== 1) return null;
  const upper = key.toUpperCase();
  return choices.find((c) => c.shortcut !== null && c.shortcut.toUpperCase() === upper)?.id ?? null;
}
