// The icon set is generated (scripts/gen-icons.mjs) — this guards against an icon the UI
// references being dropped from the subset, and against a generation that emits junk.
import { describe, expect, it } from "vitest";
import { ICONS } from "./icons";

describe("vendored icon set", () => {
  it("every entry is non-empty svg markup", () => {
    for (const [name, svg] of Object.entries(ICONS)) {
      expect(svg.startsWith("<"), `${name} starts with a tag`).toBe(true);
      expect(svg.length, `${name} has real content`).toBeGreaterThan(10);
      expect(svg.includes("script"), `${name} contains no script`).toBe(false);
    }
  });

  it("contains every icon the app currently renders", () => {
    const used = ["sun", "moon", "sun-moon", "more-horizontal", "x", "check", "chevron-down", "chevron-right"] as const;
    for (const n of used) expect(ICONS[n], n).toBeTruthy();
  });
});
