import { randomBytes } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { defineConfig } from "@playwright/test";

/**
 * End-to-end tests against the REAL API (`tests/e2e_api.py`: the real HTTP server, event
 * store, demo guard and run manager, with scripted models and the host runner, no Docker).
 * One worker, no retries. 127.0.0.1 everywhere. The access code is random for each run and
 * only ever passed through the environment: it is never written to a file or printed.
 */
export const REAL_API_PORT = 8010;
export const REAL_PANEL_PORT = 3010;

process.env["E2E_REAL_CODE"] ??= randomBytes(18).toString("hex");
process.env["E2E_REAL_DATA"] ??= mkdtempSync(path.join(tmpdir(), "sandcage-e2e-"));

const api = `http://127.0.0.1:${REAL_API_PORT}`;
const panel = `http://127.0.0.1:${REAL_PANEL_PORT}`;

export default defineConfig({
  testDir: "e2e-real",
  testMatch: "*.real.ts",
  workers: 1,
  retries: 0,
  fullyParallel: false,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: { baseURL: panel, viewport: { width: 1440, height: 900 }, trace: "off" },
  webServer: [
    {
      command: "uv run --locked python -m tests.e2e_api",
      cwd: "..",
      url: `${api}/healthz`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        DEMO_ACCESS_CODE: process.env["E2E_REAL_CODE"] ?? "",
        PANEL_ORIGIN: panel,
        SANDCAGE_API_PORT: String(REAL_API_PORT),
        SANDCAGE_DATA_DIR: process.env["E2E_REAL_DATA"] ?? "",
        DEMO_RUNS_PER_IP_PER_HOUR: "10000",
        DEMO_RUNS_PER_DAY: "10000",
        DEMO_MAX_CONCURRENT_RUNS: "4",
      },
    },
    {
      command: `npx next build && npx next start --hostname 127.0.0.1 --port ${REAL_PANEL_PORT}`,
      url: panel,
      reuseExistingServer: false,
      timeout: 240_000,
      env: { NEXT_DIST_DIR: ".next-e2e-real", NEXT_PUBLIC_API_URL: api },
    },
  ],
});
