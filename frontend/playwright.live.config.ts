import { randomBytes } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { defineConfig } from "@playwright/test";

/**
 * The one live run: the real API with the real provider (key read by the API process from
 * SANDCAGE_KEY_FILE, never by this config or the spec), real Docker, the real panel build.
 * Hard limits: 60 000 tokens for the run, 100 000 for the day, two runs a day. The access
 * code is random and only passed through the environment.
 */
export const LIVE_API_PORT = 8020;
export const LIVE_PANEL_PORT = 3020;

process.env["E2E_LIVE_CODE"] ??= randomBytes(18).toString("hex");
process.env["E2E_LIVE_DATA"] ??= mkdtempSync(path.join(tmpdir(), "sandcage-live-"));

const api = `http://127.0.0.1:${LIVE_API_PORT}`;
const panel = `http://127.0.0.1:${LIVE_PANEL_PORT}`;

export default defineConfig({
  testDir: "e2e-live",
  testMatch: "*.live.ts",
  workers: 1,
  retries: 0,
  fullyParallel: false,
  timeout: 600_000,
  expect: { timeout: 30_000 },
  reporter: [["list"]],
  use: { baseURL: panel, viewport: { width: 1440, height: 900 }, trace: "off" },
  webServer: [
    {
      command: "uv run --locked python scripts/api_with_key_file.py",
      cwd: "..",
      url: `${api}/healthz`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        DEMO_ACCESS_CODE: process.env["E2E_LIVE_CODE"] ?? "",
        PANEL_ORIGIN: panel,
        SANDCAGE_API_PORT: String(LIVE_API_PORT),
        SANDCAGE_DATA_DIR: process.env["E2E_LIVE_DATA"] ?? "",
        LLM_PROVIDER: "vultr",
        LLM_MODEL: "deepseek-v4.1-flash-1017",
        LLM_BASE_URL: "https://api.vultrinference.com/v1",
        // Abort on the first 401, 403 or 429 (and any other failing call): no retries, no loop.
        LLM_RETRIES: "0",
        DEMO_RUN_TOKEN_CAP: "60000",
        DEMO_DAY_TOKEN_CAP: "100000",
        DEMO_RUNS_PER_DAY: "2",
        DEMO_RUNS_PER_IP_PER_HOUR: "2",
        DEMO_MAX_CONCURRENT_RUNS: "1",
      },
    },
    {
      command: `npx next build && npx next start --hostname 127.0.0.1 --port ${LIVE_PANEL_PORT}`,
      url: panel,
      reuseExistingServer: false,
      timeout: 240_000,
      env: { NEXT_DIST_DIR: ".next-e2e-live", NEXT_PUBLIC_API_URL: api },
    },
  ],
});
