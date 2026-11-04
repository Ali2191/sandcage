import { banner, expect, shot, test } from "./helpers";

test("clarify: the question banner takes a typed answer", async ({ page, api }) => {
  const id = await api.start("clarify");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toContainText("A QUESTION BEFORE THE AGENT STARTS");
  const box = page.getByRole("textbox", { name: "Is the discount a percentage off (10 means 10%) or a fixed amount off?" });
  await expect(box).toBeFocused();
  // Single-key shortcuts must not fire while typing.
  await box.pressSequentially("a d r k");
  await expect(box).toHaveValue("a d r k");
  await expect(page.getByRole("button", { name: "Answer" })).toBeEnabled();
  await box.fill("A percentage off: 10 means 10%.");
  await shot(page, "real-live-clarify");
  await page.getByRole("button", { name: "Answer" }).click();
  // Having understood the task, the agent proposes a plan, which is the second question.
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  await page.keyboard.press("a");
  await expect(page.getByRole("button", { name: "Download report" })).toBeVisible();
  await expect(page.getByText("A percentage off: 10 means 10%.")).toHaveCount(0); // the answer is not echoed as a headline
  await expect(page.getByRole("heading", { level: 1 })).toContainText("The run finished inside the cage, and the task got done.");
});

test("clarify: Stop the run ends it as needing clarification", async ({ page, api }) => {
  const id = await api.start("clarify");
  await page.goto(`/runs/${id}`);
  await page.getByRole("button", { name: "Stop the run" }).click();
  await expect(page.getByText("Needs an answer", { exact: true })).toBeVisible();
});
