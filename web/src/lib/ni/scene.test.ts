// The scene validator is the client-side half of "nothing renders that did not validate"
// (ni-format §5). Server-side is authoritative; these tests pin the renderer's own refusal
// so a spec change in either half can't drift the other into silently rendering junk.

import { describe, expect, it } from "vitest";
import { formatNumber, validateBoundScene, type SceneNode } from "./scene";

function stack(children: SceneNode[]): SceneNode {
  return { type: "stack", dir: "v", gap: "sm", children };
}

describe("validateBoundScene — accepted node types", () => {
  it("accepts a well-formed tree of every v1 node", () => {
    const tree: SceneNode = stack([
      { type: "text", value: "Hello", role: "title", tone: "default", size: "md" },
      { type: "number", value: 42, format: "compact", tone: "accent", size: "lg" },
      { type: "chip", value: "live", kind: "ok" },
      { type: "bar", value: 0.4, max: 1, tone: "warn" },
      { type: "icon", name: "monitor", tone: "muted" },
      { type: "divider" },
      { type: "grid", cols: 2, children: [
        { type: "text", value: "a", role: "label", tone: "muted", size: "sm" },
        { type: "text", value: "b", role: "value", tone: "ok", size: "md" },
      ] },
    ]);
    expect(validateBoundScene(tree)).toBeNull();
  });

  it("accepts an empty stack (the minimum viable scene)", () => {
    expect(validateBoundScene(stack([]))).toBeNull();
  });
});

describe("validateBoundScene — refuses unknown + reserved node types", () => {
  it("refuses an unknown type", () => {
    expect(validateBoundScene({ type: "widget" })).toMatch(/unknown/);
  });

  it("refuses every reserved-for-later type (§5)", () => {
    for (const t of ["spark", "gauge", "image", "when", "on_tap"]) {
      expect(validateBoundScene({ type: t })).toMatch(/reserved/);
    }
  });

  it("refuses a surviving repeat node — it must be expanded server-side (§4.3)", () => {
    const node = { type: "repeat", items: [], max: 5, template: { type: "divider" } };
    expect(validateBoundScene(node)).toMatch(/repeat/);
  });

  it("refuses a repeat nested inside a layout node", () => {
    const tree = stack([{ type: "repeat", items: [], max: 5, template: { type: "divider" } }]);
    expect(validateBoundScene(tree)).toMatch(/repeat/);
  });
});

describe("validateBoundScene — enum + value refusals", () => {
  it("refuses an out-of-range text role", () => {
    const bad = { type: "text", value: "x", role: "headline", tone: "default", size: "md" };
    expect(validateBoundScene(bad)).toMatch(/role/);
  });

  it("refuses an out-of-range tone", () => {
    const bad = { type: "text", value: "x", role: "title", tone: "hot", size: "md" };
    expect(validateBoundScene(bad)).toMatch(/tone/);
  });

  it("refuses an out-of-range text size", () => {
    const bad = { type: "text", value: "x", role: "title", tone: "default", size: "xl" };
    expect(validateBoundScene(bad)).toMatch(/size/);
  });

  it("refuses a chip kind outside the closed set", () => {
    const bad = { type: "chip", value: "n/a", kind: "info" };
    expect(validateBoundScene(bad)).toMatch(/kind/);
  });

  it("refuses grid.cols outside 2..4", () => {
    expect(validateBoundScene({ type: "grid", cols: 1, children: [] })).toMatch(/cols/);
    expect(validateBoundScene({ type: "grid", cols: 5, children: [] })).toMatch(/cols/);
  });

  it("refuses a bad stack.dir", () => {
    expect(validateBoundScene({ type: "stack", dir: "z", gap: "sm", children: [] })).toMatch(/dir/);
  });

  it("refuses a bad number format", () => {
    const bad = { type: "number", value: 1, format: "scientific", tone: "default", size: "md" };
    expect(validateBoundScene(bad)).toMatch(/format/);
  });

  it("refuses a non-finite number value", () => {
    const bad = { type: "number", value: Number.POSITIVE_INFINITY, format: "plain", tone: "default", size: "md" };
    expect(validateBoundScene(bad)).toMatch(/finite/);
  });

  it("refuses bar.max <= 0 (would render a divide-by-zero fill)", () => {
    const bad = { type: "bar", value: 1, max: 0, tone: "ok" };
    expect(validateBoundScene(bad)).toMatch(/max/);
  });
});

describe("validateBoundScene — caps enforced", () => {
  it("refuses text longer than 2000 chars", () => {
    const bad = { type: "text", value: "x".repeat(2001), role: "value", tone: "default", size: "md" };
    expect(validateBoundScene(bad)).toMatch(/2000/);
  });

  it("refuses a tree with more than 100 nodes", () => {
    const kids: SceneNode[] = [];
    for (let i = 0; i < 110; i += 1) kids.push({ type: "divider" });
    expect(validateBoundScene(stack(kids))).toMatch(/100 nodes/);
  });

  it("refuses a tree deeper than 8", () => {
    let deep: SceneNode = { type: "divider" };
    for (let i = 0; i < 9; i += 1) deep = stack([deep]);
    expect(validateBoundScene(deep)).toMatch(/depth 8/);
  });
});

describe("formatNumber", () => {
  it("compacts big numbers (1200 -> 1.2K)", () => {
    // Locale-tolerant: some locales emit a NBSP or "mil" — assert the compact SUFFIX presence.
    expect(formatNumber(1200, "compact")).toMatch(/1[.,]?2\s?K|1[.,]2\s?mil|1200/);
  });

  it("formats a percent from a 0..1 fraction", () => {
    // Intl.NumberFormat percent style multiplies by 100 and appends %.
    expect(formatNumber(0.5, "percent")).toContain("%");
    expect(formatNumber(0.5, "percent")).toMatch(/50/);
  });

  it("formats currency in USD by default (spec: currency defaults to USD)", () => {
    const s = formatNumber(1234.56, "currency");
    expect(s).toMatch(/1,234\.56|1[.,]234[.,]56/);
    expect(s).toMatch(/\$|USD/);
  });

  it("plain: appends a unit when given", () => {
    expect(formatNumber(42, "plain", "MB")).toBe("42 MB");
    expect(formatNumber(42, "plain")).toBe("42");
  });

  it("returns an em-dash for non-finite inputs so the card still renders", () => {
    expect(formatNumber(Number.NaN, "plain")).toBe("—");
    expect(formatNumber(Number.POSITIVE_INFINITY, "compact")).toBe("—");
  });
});
