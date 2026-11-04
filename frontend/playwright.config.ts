import { defineConfig } from "@playwright/test";

/**
 * End-to-end tests run the real panel build against the mock API. One worker, no
 * retries: a flaky test is a bug to fix. 127.0.0.1 everywhere (never localhost: Chrome's
 * oversized localhost cookies cause HTTP 431).
 */
export const MOCK_PORT = 8001;
export const PANEL_PORT = 3001;
export const ACCESS_CODE = "sandcage-e2e-code";

export default defineConfig({
  testDir: "e2e",
  testMatch: "*.e2e.ts",
  workers: 1,
  retries: 0,
  fullyParallel: false,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PANEL_PORT}`,
    viewport: { width: 1440, height: 900 },
    trace: "off",
  },
  webServer: [
    {
      command: "node mock/main.ts",
      url: `http://127.0.0.1:${MOCK_PORT}/healthz`,
      reuseExistingServer: false,
      env: { MOCK_PORT: String(MOCK_PORT), MOCK_SPEED: "0.02", MOCK_CODE: ACCESS_CODE, MOCK_ORIGINS: `http://127.0.0.1:${PANEL_PORT}` },
    },
    {
      command: `npx next build && npx next start --hostname 127.0.0.1 --port ${PANEL_PORT}`,
      url: `http://127.0.0.1:${PANEL_PORT}`,
      reuseExistingServer: false,
      timeout: 240_000,
      env: { NEXT_DIST_DIR: ".next-e2e", NEXT_PUBLIC_API_URL: `http://127.0.0.1:${MOCK_PORT}` },
    },
  ],
});
