"use client";

import { useCallback, useState } from "react";
import { approve, ApiError } from "@/lib/api";
import { COPY } from "@/lib/copy";

export interface ApprovalState {
  sending: string | null;
  sent: boolean;
  error: string | null;
}

const IDLE: ApprovalState = { sending: null, sent: false, error: null };

/**
 * Answers the run's pending approval. State is keyed to the approval's id, so it resets
 * by itself when the next question arrives.
 */
export function useApproval(runId: string, approvalId: string | null): { state: ApprovalState; decide: (choiceId: string, answer?: string) => Promise<void> } {
  const [byApproval, setByApproval] = useState<{ id: string | null; state: ApprovalState }>({ id: null, state: IDLE });
  const state = byApproval.id === approvalId ? byApproval.state : IDLE;

  const decide = useCallback(
    async (choiceId: string, answer?: string): Promise<void> => {
      if (approvalId === null) return;
      const set = (next: ApprovalState): void => setByApproval({ id: approvalId, state: next });
      set({ sending: choiceId, sent: false, error: null });
      try {
        await approve(runId, { approval_id: approvalId, choice: choiceId, ...(answer !== undefined ? { answer } : {}) });
        set({ sending: null, sent: true, error: null });
      } catch (cause) {
        const conflict = cause instanceof ApiError && cause.status === 409;
        const message = conflict ? COPY.live.conflict : cause instanceof ApiError && cause.status === null ? cause.message : COPY.live.sendFailed;
        set({ sending: null, sent: false, error: message });
      }
    },
    [runId, approvalId],
  );

  return { state, decide };
}
