import { type APIRequestContext, type Page } from "@playwright/test";
import { API, banner, expect, test } from "./helpers";

interface RunState {
  status: string;
  pending: { id: string; kind: string } | null;
}

async function runState(request: APIRequestContext, runId: string): Promise<RunState> {
  const response = await request.get(`${API}/runs/${runId}`);
  return (await response.json()) as RunState;
}

/** Answer like the Bench's cautious reviewer until the run ends: plan yes, breaker resume,
 *  a clarifying question answered, everything else no. The API says which question is open;
 *  the page is only asked to show that same question before the answer is given. */
async function reviewUntilReport(page: Page, request: APIRequestContext): Promise<string[]> {
  const runId = new URL(page.url()).pathname.split("/").pop() ?? "";
  const asked: string[] = [];
  for (let round = 0; round < 10; round += 1) {
    const state = await expect
      .poll(async () => {
        const found = await runState(request, runId);
        return found.pending !== null || found.status !== "running" ? found : null;
      }, { timeout: 30_000 })
      .not.toBeNull()
      .then(() => runState(request, runId));
    const { pending } = state;
    if (pending === null) break;
    const question = banner(page);
    await expect(question).toHaveAttribute("data-approval-kind", pending.kind);
    asked.push(pending.kind);
    if (pending.kind === "question") {
      await question.getByRole("textbox").fill("A percentage: 10 means 10% off.");
      await question.getByRole("button", { name: "Answer" }).click();
    } else {
      await page.keyboard.press(pending.kind === "plan" ? "a" : pending.kind === "breaker" ? "r" : "d");
    }
    await expect
      .poll(async () => (await runState(request, runId)).pending?.id ?? null, { message: `${pending.kind} question was not answered`, timeout: 20_000 })
      .not.toBe(pending.id);
  }
  await expect(page.getByRole("button", { name: "Download report" }), `questions answered: ${asked.join(", ")}`).toBeVisible({ timeout: 20_000 });
  return asked;
}

test("the gallery lists the registered scenarios, with their hand-written titles", async ({ page }) => {
  await page.goto("/gallery");
  await expect(page.getByText("Showing 12 of 12")).toBeVisible();
  await expect(page.getByRole("heading", { name: "The README asks for your .env" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "A code comment for AI agents" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Usage counts that stay in memory" })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Ordinary task\s*6$/ })).toBeVisible();
  // No scoring-check numbers are dressed up as results.
  await expect(page.getByText("attack got in")).toHaveCount(0);
});

test("Run it on an attack scenario: the attack is contained and the task still gets done", async ({ page, request }) => {
  await page.goto("/gallery");
  await page.getByRole("button", { name: /Run it: A code comment for AI agents/ }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f]{12}$/);
  await expect(page.getByText("Bench · comment_exfil")).toBeVisible();
  const asked = await reviewUntilReport(page, request);
  expect(asked[0]).toBe("plan");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Sandcage blocked it");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("the task still got done");
  await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
});

test("Run it on a benign scenario: nothing is blocked or asked about", async ({ page, request }) => {
  await page.goto("/gallery");
  await page.getByRole("button", { name: /Run it: Usage counts that stay in memory/ }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f]{12}$/);
  const asked = await reviewUntilReport(page, request);
  expect(asked.filter((kind) => kind !== "plan")).toEqual([]);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("The run finished inside the cage, and the task got done.");
  await expect(page.getByRole("button", { name: "Download patch" })).toBeVisible();
});

test("Run it works for every scenario in the gallery", async ({ page, request }) => {
  const response = await request.get(`${API}/bench/scenarios`);
  expect(response.status()).toBe(200);
  const gallery = (await response.json()) as { total: number; scenarios: { id: string; title: string }[] };
  expect(gallery.total).toBe(12);
  for (const scenario of gallery.scenarios) {
    await page.goto("/gallery");
    await page.getByRole("searchbox").fill(scenario.title);
    await page.getByRole("button", { name: `Run it: ${scenario.title}` }).click();
    await expect(page).toHaveURL(/\/runs\/[0-9a-f]{12}$/);
    await expect(page.getByText(`Bench · ${scenario.id}`)).toBeVisible();
    await reviewUntilReport(page, request);
    await expect(page.getByRole("button", { name: "Download patch" }), scenario.id).toBeVisible();
  }
});
