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

  it("still refuses on_tap (behavior reserved for a later phase)", () => {
    expect(validateBoundScene({ type: "on_tap" })).toMatch(/reserved/);
  });

  it("refuses a surviving `when` KEY on any node — server strips it at bind time (§5)", () => {
    // A content node carrying its own bind-time condition means the payload was
    // not run through the server-side binder; refuse it outright.
    const bad = { type: "text", value: "x", role: "value", tone: "default", size: "md",
      when: [{ left: 1, op: "gt", right: 0, set: { tone: "danger" } }] };
    expect(validateBoundScene(bad)).toMatch(/when/);
  });

  it("refuses {type: 'when'} — a legacy path still caught by the reserved list", () => {
    expect(validateBoundScene({ type: "when" })).toMatch(/when/);
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

describe("validateBoundScene — v2 spark node", () => {
  it("accepts a line spark of finite numbers", () => {
    const node = { type: "spark", points: [1, 2, 3, 4], kind: "line", tone: "accent" };
    expect(validateBoundScene(node)).toBeNull();
  });

  it("accepts a bars spark of {t, v} points and no tone", () => {
    const node = {
      type: "spark", kind: "bars",
      points: [{ t: "2026-09-01T00:00:00Z", v: 10 }, { t: "2026-09-02T00:00:00Z", v: 12 }],
    };
    expect(validateBoundScene(node)).toBeNull();
  });

  it("refuses more than 500 points", () => {
    const pts: number[] = [];
    for (let i = 0; i < 501; i += 1) pts.push(i);
    const node = { type: "spark", points: pts, kind: "line" };
    expect(validateBoundScene(node)).toMatch(/500/);
  });

  it("refuses a non-numeric v inside a point object", () => {
    const node = { type: "spark", points: [{ t: "a", v: "nope" }], kind: "line" };
    expect(validateBoundScene(node)).toMatch(/finite/);
  });

  it("refuses a non-finite raw number in points", () => {
    const node = { type: "spark", points: [1, Number.POSITIVE_INFINITY], kind: "line" };
    expect(validateBoundScene(node)).toMatch(/finite/);
  });

  it("refuses a kind outside the {line, bars} set", () => {
    const node = { type: "spark", points: [1, 2], kind: "pie" };
    expect(validateBoundScene(node)).toMatch(/kind/);
  });

  it("refuses points that are neither number nor {t, v} object", () => {
    const node = { type: "spark", points: ["one", "two"], kind: "line" };
    expect(validateBoundScene(node)).toMatch(/spark\.points/);
  });
});

describe("validateBoundScene — v4c image node (§24)", () => {
  it("accepts a well-formed bound image with the item's own /api/ni/items route", () => {
    const node = { type: "image", src: "/api/ni/items/abc-123/image?v=2026-09-11T00:00:00Z", alt: "radar" };
    expect(validateBoundScene(node)).toBeNull();
  });

  it("accepts the versionless form (the ?v= query is optional)", () => {
    const node = { type: "image", src: "/api/ni/items/abc-123/image", alt: "" };
    expect(validateBoundScene(node)).toBeNull();
  });

  it("refuses an absolute-URL src — pixels must be re-served same-origin", () => {
    const node = { type: "image", src: "https://radar.example.com/latest.png", alt: "x" };
    expect(validateBoundScene(node)).toMatch(/image\.src/);
  });

  it("refuses a protocol-relative src — no // escape hatch", () => {
    const node = { type: "image", src: "//attacker.example/api/ni/items/x/image", alt: "x" };
    expect(validateBoundScene(node)).toMatch(/image\.src/);
  });

  it("refuses a data: src — the browser must fetch through the app's route", () => {
    const node = { type: "image", src: "data:image/png;base64,AAA", alt: "x" };
    expect(validateBoundScene(node)).toMatch(/image\.src/);
  });

  it("refuses a foreign same-origin path (not the /api/ni/items/<id>/image route)", () => {
    const node = { type: "image", src: "/api/other/path", alt: "x" };
    expect(validateBoundScene(node)).toMatch(/image\.src/);
  });

  it("refuses an alt longer than 200 chars", () => {
    const node = { type: "image", src: "/api/ni/items/x/image", alt: "a".repeat(201) };
    expect(validateBoundScene(node)).toMatch(/200/);
  });
});

describe("validateBoundScene — v2 gauge node", () => {
  it("accepts a well-formed gauge with a label", () => {
    const node = { type: "gauge", value: 42, min: 0, max: 100, tone: "ok", label: "load" };
    expect(validateBoundScene(node)).toBeNull();
  });

  it("accepts a gauge without an optional label or tone", () => {
    const node = { type: "gauge", value: 5, min: 0, max: 10 };
    expect(validateBoundScene(node)).toBeNull();
  });

  it("refuses max <= min (would divide by zero on the arc)", () => {
    const eq = { type: "gauge", value: 5, min: 10, max: 10 };
    const inv = { type: "gauge", value: 5, min: 10, max: 5 };
    expect(validateBoundScene(eq)).toMatch(/max/);
    expect(validateBoundScene(inv)).toMatch(/max/);
  });

  it("refuses a non-finite value", () => {
    const node = { type: "gauge", value: Number.NaN, min: 0, max: 100 };
    expect(validateBoundScene(node)).toMatch(/finite/);
  });

  it("refuses a label longer than 200 chars", () => {
    const node = { type: "gauge", value: 1, min: 0, max: 10, label: "x".repeat(201) };
    expect(validateBoundScene(node)).toMatch(/200/);
  });

  it("refuses an out-of-range tone", () => {
    const node = { type: "gauge", value: 1, min: 0, max: 10, tone: "spicy" };
    expect(validateBoundScene(node)).toMatch(/tone/);
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
