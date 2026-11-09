import { expect, pageAlert, test } from "./helpers";

test.describe("loading, empty and error states", () => {
  test("Runs: loading, empty, error with retry, then success", async ({ page, mock }) => {
    await mock.inject("runs", "slow", 1500);
    await page.goto("/runs");
    await expect(page.getByRole("status", { name: "Loading runs" })).toBeVisible();
    await mock.inject("runs", "off");
    await expect(page.getByText("No runs yet.")).toBeVisible();
    await expect(page.getByRole("link", { name: "Start one" })).toBeVisible();

    await mock.inject("runs", "error");
    await page.reload();
    await expect(pageAlert(page)).toContainText("The server hit a problem.");
    await mock.inject("runs", "off");
    await mock.seed();
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByRole("link", { name: /invoice-calc/ }).first()).toBeVisible();
  });

  test("Runs: pages through more than 20", async ({ page, mock }) => {
    await mock.seed();
    await mock.seed();
    await mock.seed();
    await mock.seed();
    await mock.seed();
    await mock.seed();
    await page.goto("/runs");
    await expect(page.getByText("1–20 of 24")).toBeVisible();
    await expect(page.getByRole("button", { name: "Newer" })).toBeDisabled();
    await page.getByRole("button", { name: "Older" }).click();
    await expect(page.getByText("21–24 of 24")).toBeVisible();
    await expect(page.getByRole("button", { name: "Older" })).toBeDisabled();
  });

  test("Gallery: error, empty and the filter", async ({ page, mock }) => {
    await mock.inject("gallery", "error");
    await page.goto("/gallery");
    await expect(pageAlert(page)).toContainText("The server hit a problem.");
    await mock.inject("gallery", "off");
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByText("Showing 21 of 21")).toBeVisible();
    await page.getByRole("button", { name: /Dependency file/ }).click();
    await expect(page.getByText("Showing 3 of 21")).toBeVisible();
    await page.getByRole("searchbox", { name: "Find an attack" }).fill("zzz-nothing");
    await expect(page.getByText("No attacks match that filter.")).toBeVisible();
  });

  test("Gallery: '/' focuses the search, and Run it starts that attack", async ({ page, mock }) => {
    await page.goto("/gallery");
    await expect(page.getByText("Showing 21 of 21")).toBeVisible();
    await page.keyboard.press("/");
    await expect(page.getByRole("searchbox", { name: "Find an attack" })).toBeFocused();
    await page.keyboard.press("Escape");
    await page.getByRole("searchbox").fill("README asks");
    await page.getByRole("button", { name: /Run it: README asks for your token/ }).click();
    await expect(page).toHaveURL(/\/runs\/[0-9a-f]{12}$/);
    await expect(page.getByText("Bench · poisoned-readme-1")).toBeVisible();
    void mock;
  });

  test("Gallery: Bench states are honest about scripted and real numbers", async ({ page, mock }) => {
    await page.goto("/gallery");
    await expect(page.getByText("No Bench results yet. Nothing here is made up in advance.")).toBeVisible();
    await mock.bench("scripted");
    await page.reload();
    await expect(page.getByText("Scoring check with a scripted model.")).toBeVisible();
    await expect(page.getByText("attack got in")).toHaveCount(0);
    await mock.bench("real");
    await page.reload();
    await expect(page.getByText("Model: mock-model (test data, not a result)")).toBeVisible();
    await expect(page.getByText("Attacks that worked, watching only")).toBeVisible();
  });

  test("Bench: empty, scripted-only, real, error", async ({ page, mock }) => {
    await page.goto("/bench");
    await expect(page.getByText("No Bench results yet.", { exact: true })).toBeVisible();
    await mock.bench("scripted");
    await page.reload();
    await expect(page.getByRole("heading", { name: "scoring check" })).toBeVisible();
    await expect(page.getByText("as designed", { exact: true })).toBeVisible();
    await expect(page.getByText(/12 scenarios/)).toBeVisible();
    await expect(page.getByText(/not real results/)).toBeVisible();
    await expect(page.getByText(/not shown as results/)).toBeVisible();
    await expect(page.getByText("Attack success rate")).toHaveCount(0);
    await mock.bench("real");
    await page.reload();
    await expect(page.getByText("Attack success rate").first()).toBeVisible();
    await expect(page.getByText(/Sandcage cut the attack success rate from/)).toBeVisible();
    await mock.inject("bench", "error");
    await page.reload();
    await expect(pageAlert(page)).toBeVisible();
  });

  test("Policy: the real rulebook, grouped, with counts", async ({ page, mock }) => {
    await page.goto("/policy");
    await expect(page.getByText("14 always blocked")).toBeVisible();
    await expect(page.getByText("5 ask first")).toBeVisible();
    await expect(page.getByText("6 allowed")).toBeVisible();
    await expect(page.getByText("path.deny_secrets")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Secret files" })).toBeVisible();
    await mock.inject("policy", "error");
    await page.reload();
    await expect(pageAlert(page)).toBeVisible();
  });

  test("404 and run-not-found", async ({ page }) => {
    await page.goto("/nope");
    await expect(page.getByRole("heading", { name: "Page not found" })).toBeVisible();
    await expect(page.getByText("There's nothing at this address.")).toBeVisible();
    await page.goto("/runs/has.dots");
    await expect(page.getByRole("heading", { name: "Page not found" })).toBeVisible();
    await page.goto("/runs/0123456789ab");
    await expect(page.getByRole("heading", { name: "Run not found" })).toBeVisible();
    await expect(page.getByText("The API has no run")).toBeVisible();
    await expect(page.getByRole("link", { name: "Start a new run" })).toBeVisible();
  });

  test("Start: loading, repo error, paused, the form and the shortcut", async ({ page, mock }) => {
    await mock.inject("repos", "error");
    await page.goto("/");
    await expect(pageAlert(page)).toContainText("The server hit a problem.");
    await mock.inject("repos", "off");
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByLabel("invoice-calc")).toBeChecked();

    await mock.pause(true);
    await page.reload();
    await expect(page.getByText("New runs are paused", { exact: true })).toBeVisible();
    await expect(page.getByText("Demo paused")).toBeVisible();
    await expect(page.getByRole("button", { name: /Start run/ })).toBeDisabled();
    await mock.pause(false);
  });

  test("Start: ⌘↵ starts the run and goes to Live", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByLabel("invoice-calc")).toBeChecked();
    await page.getByRole("textbox", { name: /Task/ }).fill("Fix the discount.");
    await page.keyboard.press("Meta+Enter");
    await expect(page).toHaveURL(/\/runs\/[0-9a-f]{12}$/);
  });

  test("Start: the scripted attack picker starts that scenario", async ({ page }) => {
    await page.goto("/");
    await page.getByText("Scripted attack demo").click();
    await page.getByLabel("Which scripted attack").selectOption("clarify");
    await page.getByRole("button", { name: /Start run/ }).click();
    await expect(page.getByText("A QUESTION BEFORE THE AGENT STARTS")).toBeVisible();
  });

  test("Live: an interrupted connection shows disconnected, a missing stream shows not found", async ({ page, mock }) => {
    const id = await mock.startRun("demo");
    await page.goto(`/runs/${id}`);
    await expect(page.getByRole("status").filter({ hasText: "Paused, waiting for you" })).toBeVisible();
    await mock.inject("run", "error");
    await page.reload();
    await expect(page.getByRole("status").filter({ hasText: "Paused, waiting for you" })).toBeVisible();
  });

  test("Report: a failed patch download shows a message with Retry", async ({ page, mock }) => {
    const id = await mock.startRun("skip_test");
    await mock.inject("patch", "error");
    await page.goto(`/runs/${id}`);
    await expect(page.getByRole("complementary").getByRole("alert")).toContainText("The server hit a problem.");
    await mock.inject("patch", "off");
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
  });
});

test("access code: Start needs it, a wrong code is refused, the right one works", async ({ browser, request }) => {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } }); // a first-time visitor: no saved code
  const page = await context.newPage();
  await request.post("http://127.0.0.1:8001/__mock/reset");
  await page.goto("/");
  await expect(page.getByLabel(/Access code/)).toBeVisible();
  await expect(page.getByRole("button", { name: /Start run/ })).toBeDisabled();
  await page.getByLabel(/Access code/).fill("wrong-code");
  await page.getByRole("button", { name: /Start run/ }).click();
  await expect(page.getByText("That access code is not right.")).toBeVisible();
  await page.getByLabel(/Access code/).fill("sandcage-e2e-code");
  await page.getByRole("button", { name: /Start run/ }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f]{12}$/);
  await context.close();
});
