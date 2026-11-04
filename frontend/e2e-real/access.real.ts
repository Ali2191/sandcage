import { API, code, expect } from "./helpers";
import { test as base } from "@playwright/test";

/** A first-time visitor: no access code saved in the browser. */
const test = base;

test("missing code: the API answers 401 and the form will not start without one", async ({ page, request }) => {
  const response = await request.post(`${API}/runs`, { data: { repo: "invoice-calc", task: "x", guard: true, approve_plan: false } });
  expect(response.status()).toBe(401);
  await page.goto("/");
  await expect(page.getByLabel(/Access code/)).toBeVisible();
  await expect(page.getByRole("button", { name: /Start run/ })).toBeDisabled();
});

test("wrong code: refused with the panel's own words, then the right code starts a run", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel(/Access code/).fill("not-the-code-at-all");
  await page.getByRole("button", { name: /Start run/ }).click();
  await expect(page.getByText("That access code is not right.")).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
  await page.getByLabel(/Access code/).fill(code());
  await page.getByRole("button", { name: /Start run/ }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f]{12}$/);
});
