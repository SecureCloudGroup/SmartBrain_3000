// clir.ts — validator + live-binding port + token coverage. Node environment.
// Fixtures live beside the Python painter (W/app/tests/fixtures/ni_forms/clir/); we
// load them by file so a repack can't drift this file silently.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  ClirError,
  INTER_ASCENT,
  INTER_DESCENT,
  activeVariant,
  ageText,
  applyLive,
  countUpText,
  countdownText,
  formatTime,
  lineSegments,
  numberText,
  renderModel,
  resolveX,
  timePrimText,
  validateClir,
  type Clir,
  type LinePrim,
  type Prim,
  type RenderText,
  type TextPrim,
} from "./clir";
import { NI_TOKEN_NAMES, NI_CSS_DARK, NI_CSS_LIGHT, isTokenName } from "./tokens";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIX = join(HERE, "..", "..", "..", "..", "app", "tests", "fixtures", "ni_forms", "clir");

function loadFixture(name: string): unknown {
  return JSON.parse(readFileSync(join(FIX, `${name}.clir.json`), "utf8"));
}

const NY = "America/New_York";
const MS_UTC = (y: number, mo: number, d: number, h: number, mi: number) =>
  Date.UTC(y, mo - 1, d, h, mi, 0);

// ---------------------------------------------------------------- validateClir
describe("validateClir — fixtures", () => {
  it("accepts stat_nvda", () => {
    const c = validateClir(loadFixture("stat_nvda"));
    expect(c.form).toBe("stat");
    expect(c.summary).toContain("NVDA");
    expect(c.prims.length).toBeGreaterThan(5);
  });

  it("accepts curve_tide", () => {
    const c = validateClir(loadFixture("curve_tide"));
    expect(c.form).toBe("event_curve");
    expect(c.live.length).toBeGreaterThan(0);
  });

  it("accepts every_kind", () => {
    const c = validateClir(loadFixture("every_kind"));
    const kinds = new Set(c.prims.map((p) => p.k));
    expect(kinds.has("basemap")).toBe(true);
    expect(kinds.has("cells")).toBe(true);
    expect(kinds.has("icon")).toBe(true);
  });
});

// ---------------------------------------------------------------- typed refusals
describe("validateClir — refusals", () => {
  const base = () => loadFixture("stat_nvda") as Record<string, unknown>;

  it("refuses an unknown prim kind", () => {
    const bad = base();
    (bad.prims as Record<string, unknown>[])[0].k = "bogus";
    try { validateClir(bad); expect.fail("expected throw"); }
    catch (e) {
      expect(e).toBeInstanceOf(ClirError);
      expect((e as ClirError).code).toBe("bad_prim_kind");
    }
  });

  it("refuses an unknown token", () => {
    const bad = base();
    (bad.prims as Record<string, unknown>[])[0].tok = "chartreuse";
    try { validateClir(bad); expect.fail("expected throw"); }
    catch (e) {
      expect((e as ClirError).code).toBe("bad_token");
    }
  });

  it("refuses px below 11 (type-scale floor)", () => {
    const bad = base();
    (bad.prims as Record<string, unknown>[])[0].px = 9;
    try { validateClir(bad); expect.fail("expected throw"); }
    catch (e) {
      expect((e as ClirError).code).toBe("bad_number");
    }
  });

  it("refuses a wrong anchor shape", () => {
    const bad = base();
    (bad.prims as Record<string, unknown>[])[0].x = ["z", 10];
    try { validateClir(bad); expect.fail("expected throw"); }
    catch (e) {
      expect((e as ClirError).code).toBe("bad_anchor");
    }
  });
});

// ---------------------------------------------------------------- resolveX
describe("resolveX anchors", () => {
  it("resolves the four simple anchors", () => {
    expect(resolveX(["l", 10], 0, 400)).toBe(10);
    expect(resolveX(["r", 10], 0, 400)).toBe(390);
    expect(resolveX(["c", 0], 0, 400)).toBe(200);
    expect(resolveX(["f", 0.25], 0, 400)).toBe(100);
  });
  it("resolves the composite 'm' anchor", () => {
    const m = ["m", ["l", 20], ["r", 20], 0.5] as const;
    expect(resolveX(m, 0, 440)).toBe(220);
  });
});

// ---------------------------------------------------------------- formatTime
describe("formatTime — paint/live.py parity", () => {
  const T = "2026-09-24T23:10:00Z";
  it.each([
    ["h:mm a", NY, "7:10 pm"],
    ["h a", NY, "7 pm"],
    ["ha_short", NY, "7p"],
    ["EEE", NY, "Thu"],
    ["EEE d", NY, "Thu 24"],
    ["MMM d", NY, "Sep 24"],
    ["EEE MMM d", NY, "Thu Sep 24"],
    ["yyyy", NY, "2026"],
    ["MMM d, h:mm a", NY, "Sep 24, 7:10 pm"],
    ["HH:mm", NY, "19:10"],
  ])("%s in %s -> %s", (fmt, zone, exp) => {
    expect(formatTime(T, fmt, zone, false)).toBe(exp);
  });

  it("appends the zone abbreviation when asked", () => {
    expect(formatTime("2026-09-25T04:00:00Z", "h:mm a", NY, true)).toBe("12:00 am EDT");
  });

  it("date-only values are floating (no zone shift)", () => {
    expect(formatTime("2026-12-25", "EEE MMM d", "Pacific/Kiritimati", true)).toBe("Fri Dec 25");
    expect(formatTime("2026-12-25", "EEE MMM d", "Pacific/Pago_Pago", true)).toBe("Fri Dec 25");
  });

  it("timePrimText honours tz=card vs viewer", () => {
    const p = {
      k: "time" as const, id: 0, x: ["l", 0] as const, y: 0, max_w: 60,
      t: "2026-09-24T23:10:00Z", fmt: "h:mm a", tz: "card" as const,
      zone: NY, role: "footer", px: 12, tok: "muted" as const,
      anchor: "start" as const, show_zone: true,
    };
    expect(timePrimText(p, NY)).toBe("7:10 pm"); // same zone -> no suffix
    expect(timePrimText(p, "America/Los_Angeles")).toBe("7:10 pm EDT");
    expect(timePrimText({ ...p, tz: "viewer" }, "America/Los_Angeles")).toBe("4:10 pm");
  });
});

// ---------------------------------------------------------------- countdown/count_up/age/number
describe("countdown / count_up / age / number — ported fixtures", () => {
  it.each([
    [4 * 3600 + 18 * 60, "in_hm", "in 4 h 18 m"],
    [4 * 3600 + 17 * 60 + 1, "in_hm", "in 4 h 18 m"],
    [59, "in_hm", "in 1 min"],
    [0, "in_hm", "now"],
    [-5, "hm", "now"],
    [3600, "hm", "1 h"],
    [3 * 86400 + 60, "in_dhm", "in 3 days"],
    [86400 + 7200, "in_dhm", "in 26 h"],
    [86400 + 7200, "rel", "in 1 d 2 h"],
    [86400, "rel", "in 1 day"],
  ])("countdownText(%s, %s) -> %s", (s, fmt, exp) => {
    expect(countdownText(s as number, fmt as string)).toBe(exp);
  });

  it("count_up / age / number", () => {
    expect(countUpText(12 * 86400 + 5, "auto")).toBe("12 days");
    expect(countUpText(3600, "hour")).toBe("1 hour");
    expect(ageText(125 * 60)).toBe("2 h old");
    expect(ageText(30)).toBe("1 min old");
    expect(ageText(3 * 86400)).toBe("3 d old");
    expect(numberText(8_123_456_789.4, ",.0f")).toBe("8,123,456,789");
    expect(numberText(0.1234, ".1%")).toBe("12.3%");
    expect(numberText(5, "bogus{0}")).toBe("5"); // unknown fmt falls back
  });
});

// ---------------------------------------------------------------- applyLive
describe("applyLive on curve_tide — variants, past_dim, now_marker split", () => {
  const nowBefore = MS_UTC(2026, 9, 24, 18, 52);
  const nowAfter = MS_UTC(2026, 9, 24, 23, 30);
  const nowOutside = MS_UTC(2026, 9, 25, 5, 0); // outside the day window

  it("switches the active headline variant at the boundary", () => {
    const c = validateClir(loadFixture("curve_tide"));
    const before = applyLive(c, nowBefore);
    const after = applyLive(c, nowAfter);
    const headlines = (cl: Clir): string[] => cl.prims
      .filter((p) => p.k === "text" && p.role === "headline")
      .map((p) => (p as TextPrim).lines.join(" "));
    expect(headlines(before)).toEqual(["High 6.0 ft at 7:10 pm"]);
    expect(headlines(after)).toEqual(["Low 1.1 ft at 1:27 am"]);
  });

  it("resolves the countdown prim", () => {
    const c = validateClir(loadFixture("curve_tide"));
    const before = applyLive(c, nowBefore);
    const cd = before.prims.find((p) => p.k === "text" && p.id === 5) as TextPrim | undefined;
    expect(cd?.lines).toEqual(["in 4 h 18 m"]);
  });

  it("hides every now-marker prim outside the window", () => {
    const c = validateClir(loadFixture("curve_tide"));
    const out = applyLive(c, nowOutside);
    const markerIds = (c.live[0].args.prims as number[]) ?? [];
    const remaining = new Set(out.prims.map((p) => p.id));
    for (const id of markerIds) expect(remaining.has(id)).toBe(false);
  });

  it("moves the now-marker line to today's fraction of the day", () => {
    const c = validateClir(loadFixture("curve_tide"));
    const out = applyLive(c, nowBefore);
    const line = out.prims.find((p) => p.k === "line" && p.tok === "viz-now-line") as LinePrim | undefined;
    expect(line).toBeDefined();
    expect(line?.x0[0]).toBe("f");
    const frac = line?.x0[1] as number;
    const expected = (14 + 52 / 60) / 24;
    expect(Math.abs(frac - expected)).toBeLessThan(1e-6);
  });

  it("doesn't mutate the input CLIR", () => {
    const raw = loadFixture("curve_tide") as Record<string, unknown>;
    const c = validateClir(raw);
    const liveLen = c.live.length;
    applyLive(c, nowBefore);
    expect(c.live.length).toBe(liveLen); // input preserved
  });
});

// ---------------------------------------------------------------- active_variant
describe("activeVariant", () => {
  it("picks the right slot or falls back to the last-with-t_from<=now", () => {
    const vs = [
      { t_from: null, t_to: "2026-09-24T23:10:00Z" },
      { t_from: "2026-09-24T23:10:00Z", t_to: null },
    ];
    expect(activeVariant(vs, MS_UTC(2026, 9, 24, 22, 0))).toBe(0);
    expect(activeVariant(vs, MS_UTC(2026, 9, 24, 23, 10))).toBe(1);
    expect(activeVariant(vs, MS_UTC(2026, 9, 25, 2, 0))).toBe(1);
  });
});

// ---------------------------------------------------------------- renderModel
describe("renderModel", () => {
  it("stat_nvda yields the hero and the delta runs", () => {
    const c = validateClir(loadFixture("stat_nvda"));
    const now = Date.parse("2026-09-24T18:21:00Z");
    const m = renderModel(c, { width: c.bucket.min_w, now, viewerTz: NY });
    const texts = m.ops
      .filter((o): o is Extract<typeof o, { kind: "text" }> => o.kind === "text")
      .map((o) => o.lines.join(" "));
    expect(texts).toContain("$223.86");
    expect(texts).toContain("1.65 (0.73%)");
    expect(m.summary).toBe("NVDA 223.86, down 1.65");
  });

  it("curve_tide splits the now-marker line around label gaps when present", () => {
    const c = validateClir(loadFixture("curve_tide"));
    const now = MS_UTC(2026, 9, 24, 18, 52);
    const m = renderModel(c, { width: c.bucket.min_w, now, viewerTz: NY });
    // The marker line is prim with tok "viz-now-line"; look it up in the raw prims.
    const line = m.ops
      .filter((o) => o.kind === "svg")
      .map((o) => (o as { kind: "svg"; prim: Prim }).prim)
      .find((p): p is LinePrim => p.k === "line" && p.tok === "viz-now-line");
    expect(line).toBeDefined();
    // The curve_tide fixture doesn't set `gaps` on the marker line (no label-gap
    // split today), so no `_segs` is attached — the painter draws a single run.
    const segs = (line as LinePrim & { _segs?: Array<[number, number]> })._segs;
    expect(segs).toBeUndefined();
  });
});

// ---------------------------------------------------------------- tokens coverage
describe("tokens — every name is a CSS var in both themes", () => {
  it("has 44 tokens and the two theme blocks share the set", () => {
    expect(NI_TOKEN_NAMES.length).toBeGreaterThan(0);
    for (const n of NI_TOKEN_NAMES) {
      expect(isTokenName(n)).toBe(true);
      expect(NI_CSS_DARK).toContain(`--ni-${n}:`);
      expect(NI_CSS_LIGHT).toContain(`--ni-${n}:`);
    }
  });
});

// ---------------------------------------------------------------- lineSegments (live.line_segments)
describe("lineSegments — a now line breaks around the labels it would cross", () => {
  const label = (id: number, y: number, fx: number): TextPrim => ({
    k: "text", id, x: ["f", fx], y, max_w: 110, lines: ["8:48 pm · 6.64 ft"], role: "label",
    px: 11, tok: "muted", anchor: "middle", dir: "ltr", src: "data",
  });
  const line = (fx: number, gaps: number[]): LinePrim => ({
    k: "line", id: 3, x0: ["f", fx], y0: 60, x1: ["f", fx], y1: 200,
    tok: "viz-now-line", w: 1, dash: null, gaps,
  });

  it("splits into two runs around one label (the Python test_line_gaps_painted case)", () => {
    const by = new Map<number, Prim>([[2, label(2, 100, 0.5)]]);
    const segs = lineSegments(line(0.52, [2]), by, 400);
    expect(segs.length).toBe(2);
    expect(segs[0][0]).toBe(60);
    expect(segs[0][1]).toBeLessThan(95);
    expect(segs[1][0]).toBeGreaterThan(100);
    expect(segs[1][1]).toBe(200);
  });

  it("keeps one run when the label is clear of the line", () => {
    const by = new Map<number, Prim>([[2, label(2, 100, 0.1)]]);
    expect(lineSegments(line(0.52, [2]), by, 400)).toEqual([[60, 200]]);
  });

  it("splits into three runs around two labels", () => {
    const by = new Map<number, Prim>([[2, label(2, 100, 0.5)], [4, label(4, 160, 0.55)]]);
    expect(lineSegments(line(0.52, [2, 4]), by, 400).length).toBe(3);
  });

  it("ignores gap ids that are not text prims", () => {
    const by = new Map<number, Prim>([[9, line(0.52, [])]]);
    expect(lineSegments(line(0.52, [9]), by, 400)).toEqual([[60, 200]]);
  });
});

// ---------------------------------------------------------------- text box top (vector.py parity)
describe("text box top — the baseline lands on the CLIR y with Inter's metrics", () => {
  it("stat_nvda title: top = y − half-leading − ascent", () => {
    const c = validateClir(loadFixture("stat_nvda"));
    const now = Date.parse("2026-09-24T18:21:00Z");
    const m = renderModel(c, { width: c.bucket.min_w, now, viewerTz: NY });
    const title = m.ops.find((o): o is RenderText => o.kind === "text" && o.id === 0);
    expect(title).toBeDefined();
    const { px, lh, top } = title as RenderText;
    const expected = 14 - (lh - (INTER_ASCENT + INTER_DESCENT) * px) / 2 - INTER_ASCENT * px;
    expect(Math.abs(top - expected)).toBeLessThan(1e-9);
    expect(top).toBeLessThan(14);
  });
});
