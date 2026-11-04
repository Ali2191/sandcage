/**
 * What a production build for the deploy must not contain (the panel is served from the same
 * origin as the API, at /api): no URL that points at a developer's machine. Pure rules, checked
 * by scripts/check-bundle.ts against the real build output and by unit tests against samples.
 */

export interface BuiltFile {
  /** Path relative to the build directory, with forward slashes. */
  path: string;
  text: string;
}

const CLIENT_ASSET = /^static\/.*\.js$|\.(html|rsc)$/;
const LOCAL_HOST = /127\.0\.0\.1|:\/\/localhost|\blocalhost:\d|\[::1\]/;
const LOCAL_HOST_WITH_PORT = /(127\.0\.0\.1|localhost):\d{2,5}/;

/** The problems found in the build, one human-readable line each (empty when it is clean). */
export function bundleProblems(files: readonly BuiltFile[]): string[] {
  const problems: string[] = [];
  let sameOriginBase = false;
  for (const file of files) {
    if (file.path.endsWith(".map") || file.path.startsWith("cache/")) continue;
    if (CLIENT_ASSET.test(file.path)) {
      const local = LOCAL_HOST.exec(file.text);
      if (local) problems.push(`${file.path}: a local address in a file sent to the browser (${local[0]})`);
      if (file.path.endsWith(".js") && file.text.includes("NEXT_PUBLIC_API_URL")) {
        problems.push(`${file.path}: NEXT_PUBLIC_API_URL was not inlined at build time`);
      }
      if (file.path.endsWith(".js") && file.text.includes('"/api"')) sameOriginBase = true;
    }
    const withPort = LOCAL_HOST_WITH_PORT.exec(file.text);
    if (withPort) problems.push(`${file.path}: a local address with a port (${withPort[0]})`);
  }
  if (!sameOriginBase) problems.push('no client script uses "/api" as the API base (was NEXT_PUBLIC_API_URL=/api set?)');
  return problems;
}
