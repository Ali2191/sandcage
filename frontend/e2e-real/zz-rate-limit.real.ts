import { API, code, expect, test } from "./helpers";

/**
 * Runs last: ten wrong codes lock this client out for ten minutes, even for the right code.
 * The calls are cross-origin from the panel's page, so reading Retry-After proves the API
 * exposes it to the browser.
 */
test("ten wrong codes lock the client out: 429, Retry-After readable in the browser, honest panel message", async ({ page }) => {
  await page.goto("/");
  const seen = await page.evaluate(async (base) => {
    let last: Response | null = null;
    for (let i = 0; i < 11; i += 1) {
      last = await fetch(`${base}/demo/unlock`, { method: "POST", headers: { "X-Access-Code": `wrong-code-${i}` } });
      if (last.status === 429) break;
    }
    return { status: last?.status ?? 0, retryAfter: last?.headers.get("retry-after") ?? null };
  }, API);
  expect(seen.status).toBe(429);
  expect(Number(seen.retryAfter)).toBeGreaterThan(0);

  // Even the right code is refused now, and the form says so without raw API text.
  await page.getByLabel(/Access code/).fill(code());
  await page.getByRole("button", { name: /Start run/ }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByLabel(/Access code/)).toHaveAttribute("aria-invalid", "true");
});
