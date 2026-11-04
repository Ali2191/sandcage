import { expect, test as base, type Locator, type Page } from "@playwright/test";
import { ACCESS_CODE, MOCK_PORT } from "../playwright.config";

export const MOCK = `http://127.0.0.1:${MOCK_PORT}`;

/** Every test starts like a returning visitor: the access code is already saved. */
export const test = base.extend<{ mock: MockControl }>({
  page: async ({ page }, provide) => {
    await page.addInitScript(
      ([code]) => {
        try {
          localStorage.setItem("sandcage.access-code", code as string);
        } catch {
          // storage blocked: the test that needs it will say so
        }
      },
      [ACCESS_CODE],
    );
    await provide(page);
  },
  // Automatic: every test starts from a clean mock, whether or not it uses the controls.
  mock: [
    async ({ request }, provide) => {
      const control = new MockControl(request);
      await control.reset();
      await provide(control);
    },
    { auto: true },
  ],
});

export { expect };

type Request = import("@playwright/test").APIRequestContext;

export class MockControl {
  constructor(private readonly request: Request) {}
  private async post(path: string, data: unknown = {}, headers: Record<string, string> = {}): Promise<void> {
    const response = await this.request.post(`${MOCK}${path}`, { data, headers });
    expect(response.ok(), `${path} -> ${response.status()}`).toBe(true);
  }
  reset(): Promise<void> {
    return this.post("/__mock/reset");
  }
  inject(target: string, mode: "error" | "empty" | "slow" | "off", ms?: number): Promise<void> {
    return this.post("/__mock/inject", { target, mode, ms });
  }
  pause(paused: boolean): Promise<void> {
    return this.post("/__mock/pause", { paused });
  }
  bench(kind: "scripted" | "real" | "none"): Promise<void> {
    return this.post("/__mock/bench", { kind });
  }
  seed(): Promise<void> {
    return this.post("/__mock/seed");
  }
  /** Start a scripted run through the API, like the Start form does. */
  async startRun(scenario: "demo" | "sneaky_urllib" | "skip_test" | "rollback" | "clarify", opts: { approvePlan?: boolean } = {}): Promise<string> {
    const response = await this.request.post(`${MOCK}/runs`, {
      data: { repo: "invoice-calc", fake_scenario: scenario, guard: true, approve_plan: opts.approvePlan ?? false },
      headers: { "X-Access-Code": ACCESS_CODE },
    });
    expect(response.status()).toBe(202);
    return ((await response.json()) as { id: string }).id;
  }
}

export const banner = (page: Page): Locator => page.locator("section[data-approval-kind]");
export const feed = (page: Page): Locator => page.getByTestId("feed");

/** Scroll to the top before a full-page screenshot (a sticky bar is captured where it sits). */
export async function shot(page: Page, name: string): Promise<void> {
  if (process.env["SANDCAGE_SCREENSHOTS"] !== "1") return;
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    document.querySelectorAll(".sc-main, .sc-aside, .sc-feed").forEach((el) => (el.scrollTop = 0));
  });
  const phone = (page.viewportSize()?.width ?? 1440) < 500;
  await page.screenshot({ path: `screens/${name}.png`, fullPage: phone });
}

/** Errors shown by the page itself (not Next's route announcer, which is also an alert). */
export const pageAlert = (page: Page): Locator => page.locator("main").getByRole("alert");
