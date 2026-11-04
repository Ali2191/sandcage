/**
 * The demo's access code, remembered in this browser so a visitor types it once. A
 * per-viewer convenience only: the server checks the code on every changing request, so
 * a missing or stale value just means the next start asks again. Storage can throw
 * (private windows, blocked site data), so every access is wrapped.
 */
const KEY = "sandcage.access-code";

export function accessCode(): string | null {
  try {
    const value = globalThis.localStorage?.getItem(KEY) ?? null;
    return value !== null && value !== "" ? value : null;
  } catch {
    return null; // storage unavailable: behave as if no code was saved
  }
}

export function rememberAccessCode(code: string): void {
  try {
    globalThis.localStorage?.setItem(KEY, code);
  } catch {
    // storage unavailable: the code simply is not remembered
  }
}
