import { banner, expect, shot, test } from "./helpers";

test("sneaky patch: diff guard asks, red-team withholds, a human releases", async ({ page, api }) => {
  const id = await api.start("sneaky_urllib");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  await page.keyboard.press("a");

  // The diff guard asks. A careless "Allow once" lets only this one edit through.
  await expect(banner(page)).toContainText("NEEDS YOUR CALL");
  await expect(banner(page)).toContainText("The agent wants to do something the plan doesn't cover.");
  await page.keyboard.press("a");

  // The red-team names the finding and has no keyboard shortcut.
  await expect(banner(page)).toContainText("The red-team found high-severity issues. Release the patch anyway?");
  await expect(banner(page)).toContainText("invoice_calc/invoice.py:46");
  await page.keyboard.press("r");
  await page.keyboard.press("k");
  await expect(banner(page)).toBeVisible();
  await page.getByRole("button", { name: "Keep it blocked" }).click();

  // The report says withheld, never delivered.
  const headline = page.getByRole("heading", { level: 1 });
  await expect(headline).toContainText("the patch is withheld until you review it");
  await expect(headline).not.toContainText("the task got done");
  await expect(page.getByText("Withheld", { exact: true })).toBeVisible();
  await expect(page.getByText("Attacker review: 1 finding")).toBeVisible();
  await expect(page.getByText("The patch is withheld", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Download patch" })).toHaveCount(0);
  await shot(page, "real-report-sneaky-withheld");

  // Release it.
  await page.getByRole("button", { name: "Release patch" }).click();
  await expect(page.getByText("released by you", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
  await expect(page.getByText("Released by you", { exact: true })).toBeVisible();
  await expect(headline).toContainText("you released the patch");
  await shot(page, "real-report-sneaky-released");

  // Still released after a reload.
  await page.reload();
  await expect(page.getByText("released by you", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
});

test("denying the sneaky edit ends the run honestly: declined, not blocked", async ({ page, api }) => {
  const id = await api.start("sneaky_urllib");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toContainText("APPROVE THE PLAN");
  await page.keyboard.press("a");
  await expect(banner(page)).toContainText("NEEDS YOUR CALL");
  await page.keyboard.press("d");
  // A refused ask is not a gate denial: the headline does not claim Sandcage blocked anything,
  // and the declined line says what happened.
  const headline = page.getByRole("heading", { level: 1 });
  await expect(headline).toHaveText("The run ended before the task was done.");
  await expect(headline).not.toContainText("blocked");
  await expect(page.getByTestId("declined-line")).toHaveText("You did not approve 1 request, so it did not run.");
  // What the API says happened is what the page shows: verification failed, and the run still
  // offers the patch it has (the human reads it before using it), so the card must be there.
  const row = await api.row(id);
  expect(row["status"]).toBe("verify_failed");
  expect(row["delivered"]).toBe(true);
  await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
});
