// L2 frontier-repair helpers — the "Fix proposed" review modal on /ni relies on
// stagesFromSpec to slice out the CURRENT extract/transform from an item spec so
// the diff view can render current vs proposed side by side. Every branch the
// modal can hit off the happy path is exercised here.

import { describe, expect, it } from "vitest";
import { formatStageJson, stagesFromSpec } from "./proposal";

describe("stagesFromSpec", () => {
  it("returns both stages when the spec's pipeline has extract + transform", () => {
    const spec = {
      pipeline: [
        { op: "extract", paths: { price: "quote.latest", rows: "items[0:5]" } },
        { op: "transform", apply: [{ fn: "round", field: "price", digits: 2 }] },
      ],
    };
    const s = stagesFromSpec(spec);
    expect(s.extract).toEqual({ price: "quote.latest", rows: "items[0:5]" });
    expect(s.transform).toEqual([{ fn: "round", field: "price", digits: 2 }]);
  });

  it("returns null for an absent extract stage (transform-only pipeline)", () => {
    const spec = { pipeline: [{ op: "transform", apply: [] }] };
    const s = stagesFromSpec(spec);
    expect(s.extract).toBeNull();
    expect(s.transform).toEqual([]);
  });

  it("returns null for an absent transform stage (extract-only pipeline)", () => {
    const spec = { pipeline: [{ op: "extract", paths: { x: "a.b" } }] };
    const s = stagesFromSpec(spec);
    expect(s.extract).toEqual({ x: "a.b" });
    expect(s.transform).toBeNull();
  });

  it("ignores unrelated pipeline ops (e.g. llm) — the L2 surface only shows extract/transform", () => {
    const spec = {
      pipeline: [
        { op: "extract", paths: { x: "a" } },
        { op: "llm", instruction: "summarize", output: {} },
      ],
    };
    const s = stagesFromSpec(spec);
    expect(s.extract).toEqual({ x: "a" });
    expect(s.transform).toBeNull();
  });

  it("returns null-null for null / undefined / non-object / array spec inputs", () => {
    expect(stagesFromSpec(null)).toEqual({ extract: null, transform: null });
    expect(stagesFromSpec(undefined)).toEqual({ extract: null, transform: null });
    // Casts model the api's unstructured Record<string, unknown> spec surface.
    expect(stagesFromSpec({} as Record<string, unknown>)).toEqual({ extract: null, transform: null });
    expect(stagesFromSpec({ pipeline: "nope" } as unknown as Record<string, unknown>))
      .toEqual({ extract: null, transform: null });
  });

  it("keeps the FIRST extract/transform when the pipeline oddly repeats one (defensive; v1 has at most one)", () => {
    const spec = {
      pipeline: [
        { op: "extract", paths: { a: "one" } },
        { op: "extract", paths: { b: "two" } },
      ],
    };
    expect(stagesFromSpec(spec).extract).toEqual({ a: "one" });
  });

  it("skips a malformed extract stage (paths not an object) and treats it as absent", () => {
    const spec = {
      pipeline: [{ op: "extract", paths: "not-an-object" }],
    };
    expect(stagesFromSpec(spec).extract).toBeNull();
  });
});

describe("formatStageJson", () => {
  it("pretty-prints an object with 2-space indent (matches the internal docs)", () => {
    expect(formatStageJson({ x: 1 })).toBe("{\n  \"x\": 1\n}");
  });

  it("renders '(none)' for null so the modal doesn't paint a bare 'null'", () => {
    expect(formatStageJson(null)).toBe("(none)");
    expect(formatStageJson(undefined)).toBe("(none)");
  });

  it("pretty-prints an array (the transform apply-list)", () => {
    const out = formatStageJson([{ fn: "round" }]);
    expect(out).toContain("\"fn\": \"round\"");
  });
});
