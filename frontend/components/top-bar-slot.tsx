"use client";

import { useSyncExternalStore, type ReactNode } from "react";
import { createPortal } from "react-dom";

export const TOP_BAR_SLOT_ID = "top-bar-slot";

const subscribe = (): (() => void) => () => undefined;
const find = (): HTMLElement | null => document.getElementById(TOP_BAR_SLOT_ID);
const none = (): HTMLElement | null => null;

/** Render children into the right side of the top bar (the page's context). */
export function TopBarSlot({ children }: { children: ReactNode }) {
  const target = useSyncExternalStore(subscribe, find, none);
  return target ? createPortal(children, target) : null;
}
