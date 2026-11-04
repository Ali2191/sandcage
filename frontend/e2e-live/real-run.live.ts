import { writeFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { LIVE_API_PORT } from "../playwright.live.config";

const API = `http://127.0.0.1:${LIVE_API_PORT}`;
const OUT = "screens/real-run";
const MAX_MS = 8 * 60_000;

interface Seen {
  kind: string;
  headline: string;
  answered: string;
}

async function shoot(page: Page, name: string): Promise<void> {
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    document.querySelectorAll(".sc-main, .sc-aside, .sc-feed").forEach((el) => (el.scrollTop = 0));
  });
  const phone = (page.viewportSize()?.width ?? 1440) < 500;
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: phone });
}

/**
 * Records what a real model does through the panel. It asserts nothing about the model's
 * behaviour: only that the panel could be driven and the facts were written down.
 * Plan questions are approved; every other question is denied (the safe answer).
 */
test("one real run through the panel", async ({ page, request }) => {
  const code = process.env["E2E_LIVE_CODE"] ?? "";
  await page.goto("/");
  await page.getByLabel(/Access code/).fill(code);
  await expect(page.getByText("invoice-calc").first()).toBeVisible();
  await page.getByRole("button", { name: /Start run/ }).click();
  await page.waitForURL(/\/runs\/[0-9a-f]{12}$/);
  const id = /\/runs\/([0-9a-f]{12})$/.exec(page.url())?.[1] ?? "";

  const seen: Seen[] = [];
  const started = Date.now();
  let shot = false;
  const banner = page.locator("section[data-approval-kind]");
  while (Date.now() - started < MAX_MS) {
    const row = (await (await request.get(`${API}/runs/${id}`)).json()) as { status: string; pending: { id: string; kind: string; headline: string } | null };
    if (row.status !== "running") break;
    if (row.pending && (await banner.count()) === 1) {
      const kind = row.pending.kind;
      if (!shot) {
        shot = true;
        await shoot(page, `live-${kind}-1440`);
        await page.setViewportSize({ width: 390, height: 844 });
        await shoot(page, `live-${kind}-390`);
        await page.setViewportSize({ width: 1440, height: 900 });
      }
      const approve = kind === "plan";
      seen.push({ kind, headline: row.pending.headline, answered: approve ? "approved" : "denied" });
      await page.keyboard.press(approve ? "a" : "d");
      await expect(banner).toHaveCount(0, { timeout: 30_000 }).catch(() => undefined);
    }
    await page.waitForTimeout(1000);
  }

  const final = (await (await request.get(`${API}/runs/${id}`)).json()) as Record<string, unknown>;
  const events = (await (await request.get(`${API}/runs/${id}/events`)).text())
    .split("\n")
    .filter((line) => line.startsWith("data: "))
    .map((line) => JSON.parse(line.slice(6)) as Record<string, unknown>);
  const report = events.find((e) => e["kind"] === "report");
  const done = events.find((e) => e["kind"] === "done");
  const budget = (done?.["budget"] ?? null) as Record<string, unknown> | null;
  const findings = {
    run: id,
    status: final["status"],
    outcome: final["outcome"],
    steps: final["steps"],
    counts: final["counts"],
    delivered: final["delivered"],
    questions: seen,
    report: report ? { outcome: report["outcome"], headline: report["headline"], declined_line: report["declined_line"], blocks: report["blocks"], declined: report["declined"] } : null,
    tokens: budget?.["tokens"] ?? null,
    prompt_tokens: budget?.["prompt_tokens"] ?? null,
    completion_tokens: budget?.["completion_tokens"] ?? null,
    tokens_reported: budget?.["tokens_reported"] ?? null,
    seconds: budget?.["seconds"] ?? null,
    done_summary: done?.["summary"] ?? null,
    event_kinds: events.map((e) => e["kind"]),
  };
  writeFileSync(`${OUT}/findings.json`, JSON.stringify(findings, null, 2) + "\n");
  console.log("FINDINGS", JSON.stringify({ ...findings, event_kinds: undefined }));

  // The report page, at both widths, if the run got that far.
  await page.reload();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await shoot(page, "report-1440");
  await page.setViewportSize({ width: 390, height: 844 });
  await shoot(page, "report-390");
  const widths = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  console.log("PHONE_WIDTH", JSON.stringify(widths));
});
