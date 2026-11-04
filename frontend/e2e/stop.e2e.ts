import { banner, expect, test } from "./helpers";

test("Esc while the plan waits stops the run and nothing is delivered", async ({ page, mock }) => {
  const id = await mock.startRun("demo");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("You stopped this run. Nothing was delivered.");
  await expect(page.getByText("Stopped by you", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Download patch" })).toHaveCount(0);
  await expect(page.getByText("Your patch")).toHaveCount(0);
  const info = await (await page.request.get(`http://127.0.0.1:8001/runs/${id}`)).json();
  expect(info.status).toBe("stopped");
});

test("the Stop button at the breaker stops the run and nothing is delivered", async ({ page, mock }) => {
  const id = await mock.startRun("demo");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  await page.keyboard.press("a");
  await expect(banner(page)).toContainText("CIRCUIT BREAKER");
  await page.getByRole("button", { name: /Stop run/ }).click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText("You stopped this run. Nothing was delivered.");
  await expect(page.getByRole("button", { name: "Download report" })).toHaveCount(0);
});

test("denying the plan ends the run without starting the agent", async ({ page, mock }) => {
  const id = await mock.startRun("demo");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  await page.keyboard.press("d");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("The plan was not approved, so the agent never started.");
  await expect(page.getByText("plan_rejected", { exact: true })).toBeVisible();
});

test("ending the run at the breaker kills it", async ({ page, mock }) => {
  const id = await mock.startRun("demo");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  await page.keyboard.press("a");
  await expect(banner(page)).toContainText("CIRCUIT BREAKER");
  await page.keyboard.press("k");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("The run was ended at the circuit breaker. Nothing was delivered.");
});
