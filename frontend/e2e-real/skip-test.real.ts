import { banner, expect, test } from "./helpers";

test("skip_test: the weakened test edit is blocked in Sandcage's words", async ({ page, api }) => {
  const id = await api.start("skip_test");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  await page.keyboard.press("a");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("blocked it once");
  await expect(page.getByText("Tried to weaken the tests in tests/test_invoice.py").first()).toBeVisible();
  await expect(page.getByText("11 actions")).toHaveCount(0);
  await expect(page.getByText("Independent check passed")).toBeVisible();
});

