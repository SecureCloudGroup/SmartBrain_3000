// Geometry-only tests for the v2 spark + gauge helpers. The scene validator's
// contract lives in scene.test.ts; here we just pin the pure math so a
// component-level change can't drift the SVG output.

import { describe, expect, it } from "vitest";
import { sparkPath, sparkBars, gaugeArc } from "./sparkline";

function pairs(s: string): Array<[number, number]> {
  return s.split(" ").map((tok) => {
    const [x, y] = tok.split(",").map(Number.parseFloat);
    return [x, y] as [number, number];
  });
}

describe("sparkPath", () => {
  it("returns an empty string for an empty series (component shows an em-dash)", () => {
    expect(sparkPath([], 100, 20)).toBe("");
  });

  it("renders a two-point series across the full width", () => {
    const pts = pairs(sparkPath([0, 10], 100, 20));
    expect(pts).toHaveLength(2);
    expect(pts[0][0]).toBeCloseTo(0, 5);
    expect(pts[1][0]).toBeCloseTo(100, 5);
  });

  it("normalizes: series min sits at the bottom, series max at the top (SVG Y flipped)", () => {
    const pts = pairs(sparkPath([0, 10], 100, 20));
    expect(pts[0][1]).toBeCloseTo(20, 5);
    expect(pts[1][1]).toBeCloseTo(0, 5);
  });

  it("renders a constant series as a midline (no divide-by-zero)", () => {
    const pts = pairs(sparkPath([5, 5, 5, 5], 100, 20));
    for (const [, y] of pts) expect(y).toBeCloseTo(10, 5);
  });

  it("accepts {t, v} point objects (history series shape)", () => {
    const pts = pairs(sparkPath([{ t: "a", v: 1 }, { t: "b", v: 3 }], 100, 20));
    expect(pts).toHaveLength(2);
    expect(pts[0][1]).toBeCloseTo(20, 5);
    expect(pts[1][1]).toBeCloseTo(0, 5);
  });

  it("renders a single point at the horizontal center on the midline", () => {
    const pts = pairs(sparkPath([7], 100, 20));
    expect(pts).toHaveLength(1);
    expect(pts[0][0]).toBeCloseTo(50, 5);
    expect(pts[0][1]).toBeCloseTo(10, 5);
  });
});

describe("sparkBars", () => {
  it("returns [] for an empty series", () => {
    expect(sparkBars([], 100, 20)).toEqual([]);
  });

  it("partitions the width evenly — bar widths sum to the container width", () => {
    const bars = sparkBars([1, 2, 3, 4], 100, 20);
    const total = bars.reduce((s, b) => s + b.w, 0);
    expect(total).toBeCloseTo(100, 5);
    for (const b of bars) expect(b.w).toBeCloseTo(25, 5);
  });

  it("bar heights are normalized: min→0, max→full height", () => {
    const bars = sparkBars([0, 5, 10], 100, 20);
    expect(bars[0].h).toBeCloseTo(0, 5);
    expect(bars[2].h).toBeCloseTo(20, 5);
    // y anchors the top of the rect; a full bar sits at y = 0.
    expect(bars[2].y).toBeCloseTo(0, 5);
    expect(bars[0].y).toBeCloseTo(20, 5);
  });
});

describe("gaugeArc", () => {
  it("pct is 0 at min", () => {
    expect(gaugeArc(0, 0, 100, 40).pct).toBe(0);
  });

  it("pct is 0.5 at the midpoint", () => {
    expect(gaugeArc(50, 0, 100, 40).pct).toBe(0.5);
  });

  it("pct is 1 at max", () => {
    expect(gaugeArc(100, 0, 100, 40).pct).toBe(1);
  });

  it("clamps values beyond max down to 1", () => {
    expect(gaugeArc(500, 0, 100, 40).pct).toBe(1);
  });

  it("clamps values below min up to 0", () => {
    expect(gaugeArc(-50, 0, 100, 40).pct).toBe(0);
  });

  it("emits an SVG arc path starting with a moveto", () => {
    expect(gaugeArc(50, 0, 100, 40).d.startsWith("M")).toBe(true);
  });

  it("emits only a moveto at pct 0 — no A(rc) command", () => {
    expect(gaugeArc(0, 0, 100, 40).d).not.toMatch(/\sA\s/);
  });

  it("uses the SVG large-arc flag once sweep passes 180°", () => {
    const d = gaugeArc(100, 0, 100, 40).d;
    // sweep = 240° > 180° -> large-arc flag "1"
    expect(d).toMatch(/A 40 40 0 1 1 /);
  });
});
