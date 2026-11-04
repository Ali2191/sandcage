"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api";

export type Loaded<T> = { kind: "loading" } | { kind: "error"; message: string } | { kind: "ready"; data: T };

interface Slot<T> {
  key: string;
  attempt: number;
  value: Loaded<T>;
}

/**
 * Load something once per `key` (and again on `reload`). It shows "loading" until the
 * first answer for the current key arrives, so no state is set from inside the effect.
 */
export function useLoad<T>(load: () => Promise<T>, fallbackMessage: string, key = ""): { state: Loaded<T>; reload: () => void } {
  const [attempt, setAttempt] = useState(0);
  const [slot, setSlot] = useState<Slot<T> | null>(null);

  useEffect(() => {
    let alive = true;
    load().then(
      (data) => alive && setSlot({ key, attempt, value: { kind: "ready", data } }),
      (cause: unknown) => alive && setSlot({ key, attempt, value: { kind: "error", message: cause instanceof ApiError ? cause.message : fallbackMessage } }),
    );
    return () => {
      alive = false;
    };
  }, [load, fallbackMessage, key, attempt]);

  const reload = useCallback(() => setAttempt((a) => a + 1), []);
  const state: Loaded<T> = slot !== null && slot.key === key && slot.attempt === attempt ? slot.value : { kind: "loading" };
  return { state, reload };
}
