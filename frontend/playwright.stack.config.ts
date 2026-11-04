import { defineConfig } from "@playwright/test";

/**
 * End-to-end tests against a RUNNING deploy stack (docker-compose.yml: Caddy in front of the
 * built panel and the API, real Docker sandboxes), for the local rehearsal and for a deployed
 * URL. Nothing is started here.
 *
 *   STACK_URL=https://localhost STACK_CODE_FILE=~/.sandcage-rehearsal/secrets/code npm run e2e:stack
 *
 * The access code is read from STACK_CODE_FILE (never printed). A rehearsal uses Caddy's own
 * certificate authority, so certificate errors are ignored only when STACK_REHEARSAL=1.
 * One worker, no retries.
 */
export default defineConfig({
  testDir: "e2e-stack",
  testMatch: "*.stack.ts",
  workers: 1,
  retries: 0,
  fullyParallel: false,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  reporter: [["list"]],
  use: {
    baseURL: process.env["STACK_URL"] ?? "https://localhost",
    ignoreHTTPSErrors: process.env["STACK_REHEARSAL"] === "1",
    viewport: { width: 1440, height: 900 },
    trace: "off",
  },
});
