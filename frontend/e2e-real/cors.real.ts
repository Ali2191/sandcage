import { API, code, expect, test } from "./helpers";

/**
 * CORS in a real browser: the panel is served from its own origin (127.0.0.1:3010) and
 * calls the API on another (127.0.0.1:8010). Every call below is cross-origin, so the
 * preflight, the allowed headers and the exposed Retry-After are exercised for real.
 */
test("the panel (another origin) lists repos, reads status and starts a run through the form", async ({ page }) => {
  const calls: { url: string; status: number }[] = [];
  page.on("response", (response) => {
    if (response.url().startsWith(API)) calls.push({ url: new URL(response.url()).pathname, status: response.status() });
  });
  await page.goto("/");
  // GET /repos and GET /demo/status are cross-origin reads: the form fills in from them.
  await expect(page.getByRole("button", { name: /start/i }).first()).toBeVisible();
  await expect(page.getByText("invoice-calc").first()).toBeVisible();
  // POST /demo/unlock and POST /runs are cross-origin changing calls with X-Access-Code (preflighted).
  await page.keyboard.press("Control+Enter");
  await page.waitForURL(/\/runs\/[0-9a-f]{12}$/);
  expect(calls.filter((c) => c.url === "/runs" && c.status === 202)).toHaveLength(1);
  await expect(page.getByTestId("feed")).toBeVisible();
});

test("cross-origin answers are readable, with the custom request headers the panel sends", async ({ page, api }) => {
  await page.goto("/");
  const id = await api.start("demo");
  const seen = await page.evaluate(
    async ([base, runId, good]) => {
      // X-Access-Code and Content-Type: application/json are preflighted; a readable status
      // (even 403) means the browser accepted the CORS answer.
      const wrong = await fetch(`${base}/demo/unlock`, { method: "POST", headers: { "X-Access-Code": "not-the-code" } });
      const wrongBody = (await wrong.json()) as { detail?: string };
      const right = await fetch(`${base}/demo/unlock`, { method: "POST", headers: { "X-Access-Code": good as string } });
      // Last-Event-ID is the third allowed request header: resume the stream after event 1.
      const controller = new AbortController();
      const stream = await fetch(`${base}/runs/${runId}/events`, { headers: { "Last-Event-ID": "1" }, signal: controller.signal });
      const reader = stream.body?.getReader();
      let text = "";
      // The run waits at its plan question, so the stream stays open: read until one event arrived.
      while (reader && !/^id: \d+/m.test(text)) {
        const chunk = await reader.read();
        if (chunk.done) break;
        text += new TextDecoder().decode(chunk.value);
      }
      controller.abort();
      return { wrong: wrong.status, wrongDetail: typeof wrongBody.detail, right: right.status, stream: stream.status, firstId: /^id: (\d+)/m.exec(text)?.[1] ?? null };
    },
    [API, id, code()],
  );
  expect(seen.wrong).toBe(403);
  expect(seen.wrongDetail).toBe("string");
  expect(seen.right).toBe(200);
  expect(seen.stream).toBe(200);
  expect(Number(seen.firstId)).toBeGreaterThan(1);
});
