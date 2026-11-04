import { banner, expect, feed, shot, test } from "./helpers";

test("demo: plan approval, block, breaker, resume, report, patch", async ({ page, api }) => {
  const id = await api.start("demo");
  await page.goto(`/runs/${id}`);

  // Plan approval (high risk) with its shortcut keys.
  await expect(banner(page)).toContainText("Approve the plan before work starts, it's high risk.");
  await expect(banner(page).getByRole("button", { name: /Approve plan/ })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "Paused, waiting for you" })).toBeVisible();
  await shot(page, "real-live-plan-approval");
  await page.keyboard.press("a");

  // The planted attack is blocked, in Sandcage's own words, traced to the README.
  const blocked = feed(page).locator('[data-blocked="true"]');
  await expect(blocked).toHaveCount(2);
  await expect(blocked.first()).toContainText("Tried to send your .env to collector.example.net");
  await expect(blocked.first()).toContainText("net.deny_tools · traced to README.md");
  await expect(blocked.nth(1)).toContainText("Tried to read your secrets in .env");
  // The containment moment is shown in the top bar.
  await expect(page.getByTestId("prevented")).toContainText("Tried to send your .env to collector.example.net");

  // The breaker.
  await expect(banner(page)).toContainText("Something in README.md is steering the agent, so Sandcage paused it.");
  await expect(banner(page)).toContainText("Blocked 2 times after it read README.md.");
  await expect(banner(page)).toContainText("Resuming marks the source untrusted. Ending destroys the sandbox.");
  await shot(page, "real-live-breaker");
  await page.keyboard.press("r");

  // The report.
  await expect(page.getByRole("heading", { level: 1 })).toContainText("The agent tried to cross the line. Sandcage blocked it 2 times, and the task still got done.");
  await expect(page.getByText("Delivered", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
  await expect(page.getByText("Independent check passed")).toBeVisible();
  await shot(page, "real-report-demo");

  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download patch" }).click()]);
  expect(download.suggestedFilename()).toBe("sandcage.patch");
});
