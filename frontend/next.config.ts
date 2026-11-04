import path from "node:path";
import type { NextConfig } from "next";

// The panel imports the typed event contract from ../contract, so the bundler
// root is the repository root, not frontend/.
const repoRoot = path.join(import.meta.dirname, "..");

const config: NextConfig = {
  turbopack: { root: repoRoot },
  // The deploy image (deploy/panel.Dockerfile) builds a self-contained server.
  ...(process.env.NEXT_OUTPUT === "standalone"
    ? { output: "standalone" as const, outputFileTracingRoot: repoRoot }
    : {}),
  poweredByHeader: false,
  reactStrictMode: true,
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
};

export default config;
