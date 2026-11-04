/** Reading a unified diff for the patch card. */

export type DiffLineKind = "add" | "remove" | "marker" | "header" | "context";

export function diffLineKind(line: string): DiffLineKind {
  if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ")) return "header";
  if (line.startsWith("@@")) return "marker";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "remove";
  return "context";
}

export interface DiffFile {
  path: string;
  status: "modified" | "added" | "deleted";
}

/** Files in a git-style patch, with how each changed. */
export function diffFiles(patch: string): DiffFile[] {
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  for (const line of patch.split("\n")) {
    const header = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (header) {
      current = { path: header[2] ?? header[1] ?? "", status: "modified" };
      files.push(current);
    } else if (current && line.startsWith("--- /dev/null")) current.status = "added";
    else if (current && line.startsWith("+++ /dev/null")) current.status = "deleted";
    else if (!current && line.startsWith("+++ b/")) {
      // A patch without git headers: take the file from the +++ line.
      files.push({ path: line.slice(6), status: "modified" });
    }
  }
  return files;
}
