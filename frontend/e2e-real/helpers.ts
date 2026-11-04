import { expect, test as base, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { REAL_API_PORT } from "../playwright.real.config";

export const API = `http://127.0.0.1:${REAL_API_PORT}`;

/** The access code of this run (set by the config; never printed). */
export function code(): string {
  const value = process.env["E2E_REAL_CODE"];
  if (!value) throw new Error("E2E_REAL_CODE is not set: run through playwright.real.config.ts");
  return value;
}

export type Scenario = "demo" | "sneaky_urllib" | "skip_test" | "rollback" | "clarify";

/** Like a returning visitor: the access code is already saved in the browser. */
export const test = base.extend<{ api: Api }>({
  page: async ({ page }, provide) => {
    await page.addInitScript(
      ([saved]) => {
        try {
          localStorage.setItem("sandcage.access-code", saved as string);
        } catch {
          // storage blocked: the test that needs it will say so
        }
      },
      [code()],
    );
    await provide(page);
  },
  api: async ({ request }, provide) => {
    await provide(new Api(request));
  },
});

export { expect };

export class Api {
  constructor(private readonly request: APIRequestContext) {}

  /** Start a scripted run over HTTP, like the Start form does. */
  async start(scenario: Scenario, opts: { approvePlan?: boolean; task?: string } = {}): Promise<string> {
    const response = await this.request.post(`${API}/runs`, {
      data: { repo: "invoice-calc", fake_scenario: scenario, guard: true, approve_plan: opts.approvePlan ?? false, task: opts.task ?? "Fix the failing test." },
      headers: { "X-Access-Code": code() },
    });
    expect(response.status(), "POST /runs").toBe(202);
    return ((await response.json()) as { id: string }).id;
  }

  async row(id: string): Promise<Record<string, unknown>> {
    const response = await this.request.get(`${API}/runs/${id}`);
    expect(response.status(), `GET /runs/${id}`).toBe(200);
    return (await response.json()) as Record<string, unknown>;
  }
}

export const banner = (page: Page): Locator => page.locator("section[data-approval-kind]");
export const feed = (page: Page): Locator => page.getByTestId("feed");

/** Scroll to the top before a full-page screenshot (a sticky bar is captured where it sits). */
export async function shot(page: Page, name: string, dir = "screens/real"): Promise<void> {
  if (process.env["SANDCAGE_SCREENSHOTS"] !== "1") return;
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    document.querySelectorAll(".sc-main, .sc-aside, .sc-feed").forEach((el) => (el.scrollTop = 0));
  });
  const phone = (page.viewportSize()?.width ?? 1440) < 500;
  await page.screenshot({ path: `${dir}/${name}.png`, fullPage: phone });
}
