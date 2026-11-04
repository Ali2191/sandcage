import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  oxc: { jsx: { runtime: "automatic" } },
  resolve: {
    alias: {
      "@contract": path.resolve(import.meta.dirname, "../contract/events.ts"),
      "@": path.resolve(import.meta.dirname),
    },
  },
  test: {
    include: ["lib/**/*.test.ts", "components/**/*.test.tsx", "mock/**/*.test.ts", "scripts/**/*.test.ts"],
    environment: "node",
  },
});
