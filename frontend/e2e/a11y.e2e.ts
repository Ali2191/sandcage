import { banner, expect, test } from "./helpers";

test("a banner is announced, comes before the feed in tab order, and the countdown speaks only at thresholds", async ({ page, mock }) => {
  const id = await mock.startRun("demo");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toHaveAttribute("role", "alert");

  // Focus order: the banner's buttons come right after the Stop button, before the feed's checkbox.
  const order = await page.evaluate(() => {
    const focusable = [...document.querySelectorAll<HTMLElement>("main button, main input, main a, aside button")].filter((el) => !el.hasAttribute("disabled"));
    const names = focusable.map((el) => el.textContent?.trim() || el.getAttribute("aria-label") || el.tagName);
    return names;
  });
  const stop = order.findIndex((n) => n.startsWith("Stop run"));
  const approve = order.findIndex((n) => n.startsWith("Approve plan"));
  const follow = order.findIndex((n) => n === "INPUT");
  expect(stop).toBeGreaterThanOrEqual(0);
  expect(approve).toBeGreaterThan(stop);
  expect(follow).toBeGreaterThan(approve);

  // The spoken countdown is empty at 2:00; it is not a ticking live region.
  await expect(banner(page).getByRole("status")).toHaveText("");
  await expect(page.getByTestId("countdown")).toHaveAttribute("aria-hidden", "true");
});

test("every control shows a visible focus ring", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("button", { name: /Start run/ })).toBeVisible();
  await page.keyboard.press("Tab"); // skip link
  await page.keyboard.press("Tab");
  const outline = await page.evaluate(() => {
    const el = document.activeElement as HTMLElement;
    const style = getComputedStyle(el);
    return { width: style.outlineWidth, style: style.outlineStyle };
  });
  expect(outline.style).not.toBe("none");
  expect(parseFloat(outline.width)).toBeGreaterThanOrEqual(2);
});

test("the shortcut legend opens with ? and closes with Esc without stopping anything", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Keyboard shortcuts" })).toHaveAttribute("data-interactive", "true");
  await page.keyboard.press("?");
  const legend = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(legend).toBeVisible();
  await expect(legend).toContainText("Show this list");
  await page.keyboard.press("Escape");
  await expect(legend).toHaveCount(0);
});

test("the nav marks the current page, a run page counts as Runs", async ({ page, mock }) => {
  await page.goto("/gallery");
  await expect(page.getByRole("link", { name: "Attack gallery" })).toHaveAttribute("aria-current", "page");
  const id = await mock.startRun("skip_test");
  await page.goto(`/runs/${id}`);
  await expect(page.getByRole("link", { name: "Runs", exact: true })).toHaveAttribute("aria-current", "page");
});

test("reduced motion switches animation off", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  const duration = await page.evaluate(() => getComputedStyle(document.body).animationDuration);
  const seconds = duration.endsWith("ms") ? parseFloat(duration) / 1000 : parseFloat(duration);
  expect(seconds).toBeLessThan(0.01);
});

test("reload while an approval waits keeps the question and its deadline", async ({ page, mock }) => {
  const id = await mock.startRun("demo");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  await page.reload();
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  await expect(page.getByTestId("countdown")).toContainText(/auto-deny in [01]:\d\d/);
  await page.keyboard.press("a");
  await expect(banner(page)).toContainText("CIRCUIT BREAKER");
});
