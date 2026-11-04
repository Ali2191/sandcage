import { describe, expect, it } from "vitest";
import { bundleProblems, type BuiltFile } from "./bundle-rules.ts";

const clean: BuiltFile[] = [
  { path: "static/chunks/app.js", text: 'let base="/api".replace(/\\/+$/,"");fetch(base+"/runs")' },
  { path: "server/app/index.html", text: "<html><body>Sandcage</body></html>" },
];

describe("bundleProblems", () => {
  it("accepts a same-origin build", () => {
    expect(bundleProblems(clean)).toEqual([]);
  });

  it("refuses a local API address in a script sent to the browser", () => {
    const found = bundleProblems([...clean, { path: "static/chunks/x.js", text: 'fetch("http://127.0.0.1:8001/runs")' }]);
    expect(found.join("\n")).toContain("static/chunks/x.js");
    expect(found.join("\n")).toContain("127.0.0.1");
  });

  it("refuses localhost in prerendered pages and in RSC payloads", () => {
    for (const filePath of ["server/app/runs.html", "server/app/runs.rsc"]) {
      const found = bundleProblems([...clean, { path: filePath, text: "see http://localhost:8000/healthz" }]);
      expect(found.length, filePath).toBeGreaterThan(0);
    }
  });

  it("refuses a local address with a port anywhere in the server output", () => {
    const found = bundleProblems([...clean, { path: "server/chunks/ssr/a.js", text: 'const api="localhost:8001"' }]);
    expect(found.join("\n")).toContain("server/chunks/ssr/a.js");
  });

  it("lets the framework's own word 'localhost' through when it is not an address", () => {
    expect(bundleProblems([...clean, { path: "static/chunks/fw.js", text: 'if("localhost"===s.host&&(s.host=""))' }])).toEqual([]);
  });

  it("ignores source maps and the build cache", () => {
    const found = bundleProblems([
      ...clean,
      { path: "static/chunks/app.js.map", text: "http://127.0.0.1:8001" },
      { path: "cache/images/x.json", text: "http://127.0.0.1:8001" },
    ]);
    expect(found).toEqual([]);
  });

  it("refuses a build that did not inline the API base", () => {
    const found = bundleProblems([{ path: "static/chunks/app.js", text: 'process.env["NEXT_PUBLIC_API_URL"]' }]);
    expect(found.join("\n")).toContain("not inlined");
    expect(found.join("\n")).toContain('no client script uses "/api"');
  });

  it("refuses a build whose scripts never use /api", () => {
    expect(bundleProblems([{ path: "static/chunks/app.js", text: 'let base="https://api.example.com"' }]).join("\n")).toContain("/api");
  });
});
