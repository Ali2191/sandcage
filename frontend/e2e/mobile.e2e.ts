import type { Page } from "@playwright/test";
import { banner, expect, test } from "./helpers";

test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

/** No page may scroll sideways. Names the widest offenders so a failure is easy to fix. */
async function expectNoSidewaysScroll(page: Page, label: string): Promise<void> {
  const report = await page.evaluate(() => {
    const doc = document.documentElement;
    const width = doc.clientWidth;
    const offenders: string[] = [];
    document.querySelectorAll<HTMLElement>("body *").forEach((el) => {
      // Things that scroll inside their own box are allowed to be wider than it.
      for (let p: HTMLElement | null = el.parentElement; p; p = p.parentElement) {
        const o = getComputedStyle(p).overflowX;
        if (o === "auto" || o === "scroll" || o === "hidden") return;
      }
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > width + 1) offenders.push(`${el.tagName.toLowerCase()}.${String(el.className).slice(0, 40)} right=${Math.round(r.right)}`);
    });
    return { scrollWidth: doc.scrollWidth, width, offenders: offenders.slice(0, 5) };
  });
  expect(report.scrollWidth, `${label}: page is ${report.scrollWidth}px wide in a ${report.width}px window; widest: ${report.offenders.join("; ")}`).toBeLessThanOrEqual(report.width);
}

test("no sideways scroll on any static page", async ({ page, mock }) => {
  await mock.seed();
  await mock.bench("real");
  for (const path of ["/", "/runs", "/gallery", "/bench", "/policy", "/no-such-page", "/runs/not-a-run"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.getByRole("status", { name: /^Loading/ })).toHaveCount(0); // lists have filled in
    await expectNoSidewaysScroll(page, path);
  }
});

test("no sideways scroll in a live run, at every banner, and in the report", async ({ page, mock }) => {
  const id = await mock.startRun("demo");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  await expectNoSidewaysScroll(page, "plan banner");
  await banner(page).getByRole("button", { name: /Approve plan/ }).click();
  await expect(banner(page)).toContainText("CIRCUIT BREAKER");
  await expectNoSidewaysScroll(page, "breaker banner");
  await banner(page).getByRole("button", { name: /Resume/ }).click();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await page.getByRole("button", { name: "Show all details" }).click();
  await expectNoSidewaysScroll(page, "report with the details table open");
});

test("no sideways scroll for the question, red-team and withheld states", async ({ page, mock }) => {
  const q = await mock.startRun("clarify");
  await page.goto(`/runs/${q}`);
  await expect(banner(page)).toContainText("A QUESTION");
  await expectNoSidewaysScroll(page, "question banner");
  const s = await mock.startRun("sneaky_urllib");
  await page.goto(`/runs/${s}`);
  await expect(banner(page)).toContainText("NEEDS YOUR CALL");
  await expectNoSidewaysScroll(page, "action banner");
  await banner(page).getByRole("button", { name: /Allow once/ }).click();
  await expect(banner(page)).toContainText("RED-TEAM");
  await expectNoSidewaysScroll(page, "red-team banner");
  await banner(page).getByRole("button", { name: "Keep it blocked" }).click();
  await expect(page.getByText("The patch is withheld")).toBeVisible();
  await expectNoSidewaysScroll(page, "withheld report");
});

test("the Start action bar sits in the flow after the form, never over a field", async ({ page }) => {
  await page.goto("/");
  const start = page.getByRole("button", { name: /Start run/ });
  await expect(start).toBeVisible();
  const position = await start.evaluate((el) => {
    for (let p: Element | null = el; p; p = p.parentElement) if (getComputedStyle(p).position === "sticky" || getComputedStyle(p).position === "fixed") return getComputedStyle(p).position;
    return "static";
  });
  expect(position).toBe("static");
});
