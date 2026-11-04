import { shot, test, expect } from "./helpers";

for (const [name, viewport] of [["", { width: 1440, height: 900 }], ["m-", { width: 390, height: 844 }]] as const) {
  test.describe(`screens ${name || "desktop"}`, () => {
    test.use({ viewport });
    test("static pages", async ({ page, mock }) => {
      await mock.seed();
      await mock.bench("scripted");
      for (const [slug, path] of [["start", "/"], ["gallery", "/gallery"], ["bench", "/bench"], ["policy", "/policy"], ["runs", "/runs"]] as const) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await page.waitForLoadState("networkidle");
        await shot(page, `${name}${slug}`);
      }
    });
  });
}
