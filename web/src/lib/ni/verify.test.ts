import { describe, expect, it } from "vitest";
import { optionMaxWidth, verifyOptions } from "./verify";

const CLIR = { v: 1 }; // opaque to this module — IrPaint validates it, not verify.ts

describe("verifyOptions", () => {
  it("returns [] for null/undefined/non-object payloads", () => {
    expect(verifyOptions(null)).toEqual([]);
    expect(verifyOptions(undefined)).toEqual([]);
    expect(verifyOptions("not a payload")).toEqual([]);
    expect(verifyOptions(42)).toEqual([]);
  });

  it("returns [] for a legacy (non-form) scene payload", () => {
    expect(verifyOptions({ type: "stack", dir: "v", gap: "md", children: [] })).toEqual([]);
  });

  it("returns just the pick when there is no alternatives field (today's shape)", () => {
    const payload = { type: "form", form: "stat", clir: { desktop: CLIR, phone: CLIR }, summary: "NVDA 223.86" };
    const opts = verifyOptions(payload);
    expect(opts).toHaveLength(1);
    expect(opts[0]).toEqual({ id: "pick", clir: { desktop: CLIR, phone: CLIR }, summary: "NVDA 223.86", why: "" });
  });

  it("adds the runner-up when alternatives carries one lint-clean entry", () => {
    const second = { id: "cand2", form: "kv_grid", clir: { desktop: CLIR, phone: CLIR }, summary: "NVDA peers" };
    const payload = {
      type: "form", form: "stat", clir: { desktop: CLIR, phone: CLIR }, summary: "NVDA 223.86",
      alternatives: [second],
    };
    const opts = verifyOptions(payload);
    expect(opts).toHaveLength(2);
    expect(opts[1].id).toBe("second");
    expect(opts[1].clir).toEqual({ desktop: CLIR, phone: CLIR });
    expect(opts[1].why).toBe("Shown differently: NVDA peers");
  });

  it("falls back to a plain sentence when the runner-up has no summary", () => {
    const payload = {
      type: "form", clir: { desktop: CLIR, phone: CLIR },
      alternatives: [{ clir: { desktop: CLIR, phone: CLIR } }],
    };
    expect(verifyOptions(payload)[1].why).toBe("A different design of the same data.");
  });

  it("ignores a malformed alternatives entry (no clir) and keeps just the pick", () => {
    const payload = {
      type: "form", clir: { desktop: CLIR, phone: CLIR }, summary: "x",
      alternatives: [{ summary: "no clir here" }],
    };
    expect(verifyOptions(payload)).toHaveLength(1);
  });

  it("caps alternatives at one entry even if the payload carries more", () => {
    const payload = {
      type: "form", clir: { desktop: CLIR, phone: CLIR }, summary: "x",
      alternatives: [
        { clir: { desktop: CLIR, phone: CLIR }, summary: "a" },
        { clir: { desktop: CLIR, phone: CLIR }, summary: "b" },
      ],
    };
    const opts = verifyOptions(payload);
    expect(opts).toHaveLength(2);
    expect(opts[1].summary).toBe("a");
  });
});

describe("optionMaxWidth", () => {
  it("previews a 1-column desktop face at one board column (half the 2-column panel)", () => {
    expect(optionMaxWidth({ span: "d1x1" })).toBe("calc(50% - var(--s-2))");
    expect(optionMaxWidth({ span: "d1x2" })).toBe("calc(50% - var(--s-2))");
  });

  it("lets a 2+-column desktop face fill the panel", () => {
    expect(optionMaxWidth({ span: "d2x1" })).toBeNull();
    expect(optionMaxWidth({ span: "d2x2" })).toBeNull();
    expect(optionMaxWidth({ span: "d4x1" })).toBeNull();
  });

  it("caps a phone face at its bucket ceiling (full 398px, half 160px)", () => {
    expect(optionMaxWidth({ span: "p2x2" })).toBe("398px");
    expect(optionMaxWidth({ span: "p2x3" })).toBe("398px");
    expect(optionMaxWidth({ span: "p1x1" })).toBe("160px");
  });

  it("fills the panel without a parseable span key", () => {
    expect(optionMaxWidth({ span: "nope" })).toBeNull();
    expect(optionMaxWidth({})).toBeNull();
    expect(optionMaxWidth(null)).toBeNull();
  });
});
