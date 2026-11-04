import { readFileSync, statSync } from "node:fs";
import { expect, test as base, type Page } from "@playwright/test";

/** The access code, from the file named by STACK_CODE_FILE (mode 600). Never printed. */
export function code(): string {
  const file = process.env["STACK_CODE_FILE"];
  if (!file) throw new Error("Set STACK_CODE_FILE to a file holding the access code");
  const mode = statSync(file).mode & 0o777;
  if (mode !== 0o600 && mode !== 0o400) throw new Error(`${file} must have mode 600`);
  return readFileSync(file, "utf8").trim();
}

export interface Watched {
  /** securitypolicyviolation events the page reported. */
  violations: string[];
  /** console errors and uncaught page errors. */
  errors: string[];
}

declare global {
  interface Window {
    __csp?: string[];
  }
}

/**
 * A page for a returning visitor (the access code is saved in the browser) that records every
 * Content-Security-Policy violation, console error and uncaught error, so a test can assert
 * there were none: this is the real-browser check of the CSP Caddy sends.
 */
export const test = base.extend<{ page: Page; watched: Watched }>({
  watched: async ({}, provide) => {
    await provide({ violations: [], errors: [] });
  },
  page: async ({ page, watched }, provide) => {
    await page.addInitScript(
      ([saved]) => {
        try {
          localStorage.setItem("sandcage.access-code", saved as string);
        } catch {
          // storage blocked: the test that needs the code will say so
        }
        window.__csp = [];
        document.addEventListener("securitypolicyviolation", (event) => {
          window.__csp?.push(`${event.violatedDirective} blocked ${event.blockedURI || "inline"}`);
        });
      },
      [code()],
    );
    page.on("console", (message) => {
      if (message.type() === "error") watched.errors.push(message.text());
    });
    page.on("pageerror", (error) => watched.errors.push(error.message));
    await provide(page);
    const reported = await page.evaluate(() => window.__csp ?? []).catch(() => []);
    watched.violations.push(...reported);
  },
});

export { expect };

/** The question banner, whatever its kind. */
export const banner = (page: Page) => page.locator("section[data-approval-kind]");
export const feed = (page: Page) => page.getByTestId("feed");

/** Read the page's CSP violations now (the fixture also reads them when the test ends). */
export async function violationsNow(page: Page): Promise<string[]> {
  return page.evaluate(() => window.__csp ?? []);
}
