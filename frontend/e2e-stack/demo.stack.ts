import { banner, expect, feed, test, violationsNow } from "./helpers";

test("the scripted demo runs through Caddy, the panel and the API against real sandboxes", async ({ page, watched }) => {
  await page.goto("/gallery");
  await expect(page.getByText("Showing 12 of 12")).toBeVisible();
  await page.getByRole("button", { name: /Run it: The README asks for your \.env/ }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f]{12}$/);
  await expect(page.getByText("Bench · demo")).toBeVisible();

  // Plan approval (high risk).
  await expect(banner(page)).toContainText("Approve the plan before work starts, it's high risk.");
  await page.keyboard.press("a");

  // The planted attack is blocked, in Sandcage's own words, traced to the README.
  const blocked = feed(page).locator('[data-blocked="true"]');
  await expect(blocked).toHaveCount(2);
  await expect(blocked.first()).toContainText("Tried to send your .env to collector.example.net");
  await expect(blocked.nth(1)).toContainText("Tried to read your secrets in .env");
  await expect(page.getByTestId("prevented")).toContainText("Tried to send your .env to collector.example.net");

  // The breaker.
  await expect(banner(page)).toContainText("Something in README.md is steering the agent, so Sandcage paused it.");
  await page.keyboard.press("r");

  // The report and the patch.
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "The agent tried to cross the line. Sandcage blocked it 2 times, and the task still got done.",
  );
  await expect(page.getByText("Delivered", { exact: true })).toBeVisible();
  await expect(page.getByText("Independent check passed")).toBeVisible();
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download patch" }).click()]);
  expect(download.suggestedFilename()).toBe("sandcage.patch");

  expect(await violationsNow(page), "Content-Security-Policy violations on the live and report pages").toEqual([]);
  expect(watched.errors, "console errors").toEqual([]);
});

test("Esc on the plan banner stops the run and delivers nothing, through the edge", async ({ page, request }) => {
  await page.goto("/gallery");
  await page.getByRole("button", { name: /Run it: The README asks for your \.env/ }).click();
  await expect(banner(page)).toContainText("Approve the plan before work starts");
  await page.keyboard.press("Escape");
  await expect(page.getByText(/You stopped this run\. Nothing was delivered\./).first()).toBeVisible();
  const id = new URL(page.url()).pathname.split("/").pop() ?? "";
  const patch = await request.get(`/api/runs/${id}/patch`);
  expect(patch.status()).toBe(409);
});

test("reload at a question: the same question comes back and can still be answered", async ({ page, request }) => {
  await page.goto("/gallery");
  await page.getByRole("button", { name: /Run it: The README asks for your \.env/ }).click();
  await expect(banner(page)).toContainText("Approve the plan before work starts");
  const id = new URL(page.url()).pathname.split("/").pop() ?? "";
  const before = (await (await request.get(`/api/runs/${id}`)).json()) as { pending: { id: string } | null };
  await page.reload();
  await expect(banner(page)).toContainText("Approve the plan before work starts");
  const after = (await (await request.get(`/api/runs/${id}`)).json()) as { pending: { id: string } | null };
  expect(after.pending?.id).toBe(before.pending?.id);
  await page.keyboard.press("Escape"); // leave nothing running
  await expect(page.getByText(/You stopped this run/).first()).toBeVisible();
});

test("every page loads under the real CSP with no violation and no sideways scroll at 390 px", async ({ page, watched }) => {
  for (const [path, heading] of [
    ["/", null],
    ["/gallery", null],
    ["/bench", null],
    ["/policy", null],
    ["/runs", null],
  ] as const) {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    expect(heading).toBeNull();
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 900 });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, `${path} at ${width}px scrolls sideways`).toBeLessThanOrEqual(0);
    }
    expect(await violationsNow(page), `CSP violations on ${path}`).toEqual([]);
  }
  expect(watched.errors, "console errors").toEqual([]);
});

test("the unlock flow: a wrong code is refused by the real API behind Caddy", async ({ browser }) => {
  const context = await browser.newContext({ ignoreHTTPSErrors: process.env["STACK_REHEARSAL"] === "1" });
  const page = await context.newPage();
  const base = process.env["STACK_URL"] ?? "https://localhost";
  const response = await page.request.post(`${base}/api/demo/unlock`, { data: {}, headers: { "X-Access-Code": "definitely-not-the-code-0000" } });
  expect(response.status()).toBe(403);
  const missing = await page.request.post(`${base}/api/demo/unlock`, { data: {} });
  expect(missing.status()).toBe(401);
  await context.close();
});

test("the policy is enforced in the browser, and the watcher can see a violation", async ({ page }) => {
  await page.goto("/");
  // A picture from another origin is outside img-src: the browser must refuse it and report it.
  // (Resource loads are policed whoever started them; page.evaluate itself is exempt for eval.)
  await page.evaluate(() => {
    const image = document.createElement("img");
    image.src = "https://example.com/not-allowed.png";
    document.body.appendChild(image);
  });
  await expect.poll(() => violationsNow(page)).toContainEqual(expect.stringContaining("img-src"));
});
