import { banner, expect, test } from "./helpers";

test("reload at the plan question: the same question comes back and still works", async ({ page, api }) => {
  const id = await api.start("demo");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  // The server holds the open question: this is what a reloaded page reads.
  const before = (await api.row(id))["pending"] as { id: string; kind: string } | null;
  expect(before?.kind).toBe("plan");

  await page.reload();
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  expect(((await api.row(id))["pending"] as { id: string } | null)?.id).toBe(before?.id);
  await page.keyboard.press("a");

  // The next question (the breaker) also survives a reload, and answering it finishes the run.
  await expect(banner(page)).toContainText("CIRCUIT BREAKER");
  await page.reload();
  await expect(banner(page)).toContainText("CIRCUIT BREAKER");
  await page.keyboard.press("r");
  await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
  expect((await api.row(id))["pending"]).toBeNull();
});
