/**
 * Checks the panel's fixed wording against the design note (Design/PAGES.md).
 * Usage: node scripts/wording-verify.ts "<path to PAGES.md>"
 * Prints every catalogue string that does not appear in the note, so each can be judged:
 * a miss is either drift (fix the copy) or a deliberate addition (listed in ADDITIONS).
 */
import { readFileSync } from "node:fs";
import { COPY } from "../lib/copy.ts";

/** Strings the panel adds on purpose; each is explained in the report to the owner. */
const ADDITIONS = new Set<string>([
  "Released by you",
  "Withheld",
  "Keyboard shortcuts",
  "The red-team found high-severity issues. Release the patch anyway?",
]);

const path = process.argv[2];
if (!path) throw new Error("Pass the path to PAGES.md");
const norm = (s: string): string => s.replace(/[“”]/g, '"').replace(/[‘’]/g, "'").replace(/\s+/g, " ").toLowerCase();
const note = norm(readFileSync(path, "utf8"));

const leaves: string[] = [];
const walk = (value: unknown): void => {
  if (typeof value === "string") leaves.push(value);
  else if (Array.isArray(value)) value.forEach(walk);
  else if (value && typeof value === "object") Object.values(value).forEach(walk);
};
walk(COPY);

const misses = [...new Set(leaves)].filter((s) => s.length > 3 && !note.includes(norm(s)) && !ADDITIONS.has(s));
for (const m of misses) console.log(`not in PAGES.md: ${JSON.stringify(m)}`);
console.log(`${leaves.length} strings checked, ${misses.length} not found verbatim.`);
