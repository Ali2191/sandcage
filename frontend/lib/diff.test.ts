import { describe, expect, it } from "vitest";
import { diffFiles, diffLineKind } from "./diff";

describe("diff", () => {
  it("classifies lines", () => {
    expect(diffLineKind("+++ b/x.py")).toBe("header");
    expect(diffLineKind("--- a/x.py")).toBe("header");
    expect(diffLineKind("diff --git a/x b/x")).toBe("header");
    expect(diffLineKind("@@ -1,3 +1,3 @@")).toBe("marker");
    expect(diffLineKind("+new")).toBe("add");
    expect(diffLineKind("-old")).toBe("remove");
    expect(diffLineKind(" same")).toBe("context");
    expect(diffLineKind("")).toBe("context");
  });

  it("lists files and how they changed", () => {
    const patch = [
      "diff --git a/a.py b/a.py",
      "--- a/a.py",
      "+++ b/a.py",
      "@@ -1 +1 @@",
      "-x",
      "+y",
      "diff --git a/new.py b/new.py",
      "--- /dev/null",
      "+++ b/new.py",
      "diff --git a/gone.py b/gone.py",
      "--- a/gone.py",
      "+++ /dev/null",
    ].join("\n");
    expect(diffFiles(patch)).toEqual([
      { path: "a.py", status: "modified" },
      { path: "new.py", status: "added" },
      { path: "gone.py", status: "deleted" },
    ]);
  });

  it("returns nothing for an empty patch", () => {
    expect(diffFiles("")).toEqual([]);
  });
});
