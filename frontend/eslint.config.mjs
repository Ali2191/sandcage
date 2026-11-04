import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  globalIgnores([".next/**", ".next-e2e/**", ".next-e2e-real/**", ".next-e2e-live/**", ".next-deploy/**", "node_modules/**", "test-results/**", "playwright-report/**", "next-env.d.ts"]),
  {
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
      "no-empty": ["error", { allowEmptyCatch: false }],
    },
  },
]);
