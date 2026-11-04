import { banner, expect, shot, test } from "./helpers";

/** Writes the panel's screens to frontend/screens/ (only with SANDCAGE_SCREENSHOTS=1). */
for (const [prefix, viewport, mobile] of [["", { width: 1440, height: 900 }, false], ["m-", { width: 390, height: 844 }, true]] as const) {
  test.describe(`screens ${prefix || "desktop"}`, () => {
    test.use({ viewport, hasTouch: mobile, isMobile: mobile });

    test("static pages", async ({ page, mock }) => {
      await mock.seed();
      for (const [slug, path, ready] of [
        ["start", "/", "Give an agent"],
        ["runs", "/runs", "Every run on this server"],
        ["policy", "/policy", "The rulebook"],
      ] as const) {
        await page.goto(path);
        await expect(page.getByText(ready, { exact: false }).first()).toBeVisible();
        await expect(page.getByRole("status", { name: /^Loading/ })).toHaveCount(0);
        await shot(page, `${prefix}${slug}`);
      }
      await page.goto("/gallery");
      await expect(page.getByText("Showing 21 of 21")).toBeVisible();
      await shot(page, `${prefix}gallery`);
      await mock.bench("scripted");
      await page.goto("/bench");
      await expect(page.getByText(/not shown as results/)).toBeVisible();
      await shot(page, `${prefix}bench-scripted`);
      await mock.bench("real");
      await page.goto("/bench");
      await expect(page.getByText("Attack success rate").first()).toBeVisible();
      await shot(page, `${prefix}bench-real`);
      await page.goto("/gallery");
      await expect(page.getByText("attack got in").first()).toBeVisible();
      await shot(page, `${prefix}gallery-real`);
      await page.goto("/nope");
      await shot(page, `${prefix}404`);
      await page.goto("/runs/0123456789ab");
      await expect(page.getByRole("heading", { name: "Run not found" })).toBeVisible();
      await shot(page, `${prefix}run-not-found`);
    });

    test("start states", async ({ page, mock }) => {
      await mock.pause(true);
      await page.goto("/");
      await expect(page.getByText("New runs are paused", { exact: true })).toBeVisible();
      await shot(page, `${prefix}start-paused`);
      await mock.pause(false);
    });

    test("the demo run", async ({ page, mock }) => {
      const id = await mock.startRun("demo");
      await page.goto(`/runs/${id}`);
      await expect(banner(page)).toContainText("APPROVE THE PLAN");
      await shot(page, `${prefix}live-plan`);
      await banner(page).getByRole("button", { name: /Approve plan/ }).click();
      await expect(banner(page)).toContainText("CIRCUIT BREAKER");
      await shot(page, `${prefix}live-breaker`);
      await banner(page).getByRole("button", { name: /Resume/ }).click();
      await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
      await shot(page, `${prefix}report-demo`);
      await page.getByRole("button", { name: "Show all details" }).click();
      await page.getByRole("button", { name: "Hide details" }).scrollIntoViewIfNeeded();
      await shot(page, `${prefix}report-details`);
    });

    test("ask, red-team and withheld", async ({ page, mock }) => {
      const id = await mock.startRun("sneaky_urllib");
      await page.goto(`/runs/${id}`);
      await expect(banner(page)).toContainText("NEEDS YOUR CALL");
      await shot(page, `${prefix}live-ask`);
      await banner(page).getByRole("button", { name: /Allow once/ }).click();
      await expect(banner(page)).toContainText("RED-TEAM");
      await shot(page, `${prefix}live-redteam`);
      await banner(page).getByRole("button", { name: "Keep it blocked" }).click();
      await expect(page.getByText("The patch is withheld")).toBeVisible();
      await shot(page, `${prefix}report-withheld`);
      await page.getByRole("button", { name: "Release patch" }).click();
      await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
      await shot(page, `${prefix}report-released`);
    });

    test("question, stopped and the other scenarios", async ({ page, mock }) => {
      const q = await mock.startRun("clarify");
      await page.goto(`/runs/${q}`);
      await expect(banner(page)).toContainText("A QUESTION");
      await shot(page, `${prefix}live-question`);
      await page.getByRole("button", { name: "Stop the run" }).click();
      await expect(page.getByText("needs_clarification", { exact: true })).toBeVisible();
      await shot(page, `${prefix}report-needs-clarification`);

      const s = await mock.startRun("demo");
      await page.goto(`/runs/${s}`);
      await expect(banner(page)).toContainText("APPROVE THE PLAN");
      await page.getByRole("button", { name: /Stop run/ }).click();
      await expect(page.getByRole("heading", { level: 1 })).toContainText("You stopped this run");
      await shot(page, `${prefix}report-stopped`);

      for (const scenario of ["skip_test", "rollback"] as const) {
        const r = await mock.startRun(scenario);
        await page.goto(`/runs/${r}`);
        await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
        await shot(page, `${prefix}report-${scenario}`);
      }
    });
  });
}
