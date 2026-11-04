import { expect, test } from "./helpers";

test("skip_test: the weakened test edit is blocked in Sandcage's words", async ({ page, mock }) => {
  const id = await mock.startRun("skip_test");
  await page.goto(`/runs/${id}`);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("blocked it once");
  await expect(page.getByText("Tried to weaken the tests in tests/test_invoice.py").first()).toBeVisible();
  await expect(page.getByText("11 actions")).toHaveCount(0);
  await expect(page.getByText("Independent check passed")).toBeVisible();
});

test("rollback: the checkpoints card shows the restore", async ({ page, mock }) => {
  const id = await mock.startRun("rollback");
  await page.goto(`/runs/${id}`);
  await expect(page.getByRole("heading", { name: "Checkpoints" })).toBeVisible();
  await expect(page.getByText("the original repo")).toBeVisible();
  await expect(page.getByText("Restored checkpoint 0 by the agent.")).toBeVisible();
});
