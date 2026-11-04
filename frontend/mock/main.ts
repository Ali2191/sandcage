/** Entry point: `npm run mock` (env: MOCK_PORT, MOCK_SPEED, MOCK_CODE, MOCK_RUNS_PER_DAY, MOCK_SEED=1). */
import { MockServer } from "./server.ts";

const num = (name: string): number | null => {
  const raw = process.env[name];
  const value = raw === undefined || raw === "" ? NaN : Number(raw);
  return Number.isFinite(value) ? value : null;
};

const origins = (process.env["MOCK_ORIGINS"] ?? "http://127.0.0.1:3000,http://127.0.0.1:3001").split(",").map((s) => s.trim());
const server = new MockServer({
  port: num("MOCK_PORT") ?? 8001,
  speed: num("MOCK_SPEED") ?? 1,
  code: process.env["MOCK_CODE"] || null,
  runsPerDay: num("MOCK_RUNS_PER_DAY"),
  origins,
});
const port = await server.listen();
if (process.env["MOCK_SEED"] === "1") await server.seed();
console.log(`Sandcage mock API on http://127.0.0.1:${port} (scripted runs, mock data; not the real backend)`);
process.on("SIGTERM", () => void server.close().then(() => process.exit(0)));
process.on("SIGINT", () => void server.close().then(() => process.exit(0)));
