import { banner, expect, shot, test } from "./helpers";

test("sneaky patch: diff guard asks, red-team withholds, a human releases", async ({ page, mock }) => {
  const id = await mock.startRun("sneaky_urllib");
  await page.goto(`/runs/${id}`);

  // The diff guard asks. A careless "Allow once" lets only this one edit through.
  await expect(banner(page)).toContainText("NEEDS YOUR CALL");
  await expect(banner(page)).toContainText("The agent wants to do something the plan doesn't cover.");
  await page.keyboard.press("a");

  // The red-team names the finding and has no keyboard shortcut.
  await expect(banner(page)).toContainText("The red-team found high-severity issues. Release the patch anyway?");
  await expect(banner(page)).toContainText("invoice_calc/invoice.py:3");
  await page.keyboard.press("r");
  await page.keyboard.press("k");
  await expect(banner(page)).toBeVisible();
  await page.getByRole("button", { name: "Keep it blocked" }).click();

  // The report says withheld, never delivered.
  const headline = page.getByRole("heading", { level: 1 });
  await expect(headline).toContainText("withholding the patch");
  await expect(headline).not.toContainText("the task got done");
  await expect(page.getByText("Withheld", { exact: true })).toBeVisible();
  await expect(page.getByText("Attacker review: 1 finding")).toBeVisible();
  await expect(page.getByText("The patch is withheld")).toBeVisible();
  await expect(page.getByRole("button", { name: "Download patch" })).toHaveCount(0);
  await shot(page, "report-sneaky-withheld");

  // Release it.
  await page.getByRole("button", { name: "Release patch" }).click();
  await expect(page.getByText("released by you", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
  await expect(page.getByText("Released by you", { exact: true })).toBeVisible();
  await expect(headline).toContainText("the task got done");
  await shot(page, "report-sneaky-released");

  // Still released after a reload.
  await page.reload();
  await expect(page.getByText("released by you", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
});

test("denying the sneaky edit leaves a clean run", async ({ page, mock }) => {
  const id = await mock.startRun("sneaky_urllib");
  await page.goto(`/runs/${id}`);
  await expect(banner(page)).toContainText("NEEDS YOUR CALL");
  await page.keyboard.press("d");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("blocked it once");
  await expect(page.getByText("Tried to add network code to invoice_calc/invoice.py").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
});
