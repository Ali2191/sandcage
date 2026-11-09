import { test as base } from "@playwright/test";
import { API, code, expect } from "./helpers";

/** A first-time visitor, as in the reported defect: the access code is typed at step 4. */
const test = base;

test("Start form: the scripted demo starts, and the run is labelled scripted on Live and in Recent runs", async ({ page, request }) => {
  await page.goto("/");
  await page.getByText("Scripted attack demo").click();
  await page.getByLabel("Which scripted attack").selectOption({ label: "Poisoned README — the main demo" });
  await page.getByLabel(/Access code/).fill(code());

  const sent = page.waitForRequest((r) => r.method() === "POST" && new URL(r.url()).pathname === "/runs");
  await page.getByRole("button", { name: /Start run/ }).click();
  const body = (await sent).postDataJSON() as Record<string, unknown>;
  expect(body).toMatchObject({ repo: "invoice-calc", fake_scenario: "demo", guard: true });

  await expect(page).toHaveURL(/\/runs\/[0-9a-f]{12}$/);
  const id = page.url().split("/").pop() ?? "";
  try {
    await expect(page.getByText("scripted model", { exact: true })).toBeVisible();

    const row = (await (await request.get(`${API}/runs/${id}`)).json()) as { model: unknown };
    expect(row.model).toBeNull();

    await page.goto("/runs");
    const listed = page.locator(`a[href="/runs/${id}"]`);
    await expect(listed).toContainText("scripted model");
  } finally {
    await request.post(`${API}/runs/${id}/stop`, { headers: { "X-Access-Code": code() } });
  }
});
