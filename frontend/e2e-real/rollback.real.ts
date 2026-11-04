import { banner, expect, test } from "./helpers";

test("rollback: the checkpoints card shows the restore", async ({ page, api }) => {
  const id = await api.start("rollback");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  await page.keyboard.press("a");
  await expect(page.getByRole("heading", { name: "Checkpoints" })).toBeVisible();
  await expect(page.getByText("the original repo")).toBeVisible();
  await expect(page.getByText("Restored checkpoint 1 by the agent.")).toBeVisible();
});
