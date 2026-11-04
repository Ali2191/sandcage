import path from "node:path";
import type { NextConfig } from "next";

// The panel imports the typed event contract from ../contract, so the bundler
// root is the repository root, not frontend/.
const config: NextConfig = {
  turbopack: { root: path.join(import.meta.dirname, "..") },
  poweredByHeader: false,
  reactStrictMode: true,
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
};

export default config;
