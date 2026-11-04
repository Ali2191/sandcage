/**
 * Usage: node scripts/check-bundle.ts <build directory>
 * Fails (exit 1) when the production build points at a local address. `npm run build:deploy`
 * builds with NEXT_PUBLIC_API_URL=/api into .next-deploy and runs this on it.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { bundleProblems, type BuiltFile } from "./bundle-rules.ts";

const SCANNED = /\.(js|html|rsc|json|css|txt)$/;

function collect(root: string, directory: string, into: BuiltFile[]): void {
  for (const name of readdirSync(directory)) {
    const full = path.join(directory, name);
    if (statSync(full).isDirectory()) {
      collect(root, full, into);
    } else if (SCANNED.test(name)) {
      into.push({ path: path.relative(root, full).split(path.sep).join("/"), text: readFileSync(full, "utf8") });
    }
  }
}

const directory = process.argv[2];
if (!directory) throw new Error("Pass the build directory, e.g. .next-deploy");
const files: BuiltFile[] = [];
collect(directory, directory, files);
if (files.length === 0) throw new Error(`No build output found in ${directory}`);
const problems = bundleProblems(files);
if (problems.length > 0) {
  console.error(problems.join("\n"));
  process.exit(1);
}
console.log(`bundle check: ok (${files.length} files, the API base is /api)`);
