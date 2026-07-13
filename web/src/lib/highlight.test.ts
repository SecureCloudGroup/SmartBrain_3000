import { describe, expect, it } from "vitest";

import { highlight, queryTerms } from "./highlight";

const text = (segs: { t: string }[]) => segs.map((s) => s.t).join("");
const marked = (segs: { t: string; hit: boolean }[]) => segs.filter((s) => s.hit).map((s) => s.t);

describe("highlight", () => {
  it("marks matched terms and leaves the rest alone", () => {
    const segs = highlight("The lease renewal date", ["lease"]);
    expect(marked(segs)).toEqual(["lease"]);
    expect(text(segs)).toBe("The lease renewal date"); // lossless: reassembles exactly
  });

  it("matches case-insensitively but preserves the original casing", () => {
    expect(marked(highlight("The LEASE agreement", ["lease"]))).toEqual(["LEASE"]);
  });

  it("marks several terms and several occurrences", () => {
    const segs = highlight("lease renewal, lease term", ["lease", "renewal"]);
    expect(marked(segs)).toEqual(["lease", "renewal", "lease"]);
  });

  it("prefers the longest term when two overlap", () => {
    expect(marked(highlight("renewal", ["ren", "renewal"]))).toEqual(["renewal"]);
  });

  it("treats regex metacharacters in a query as literal text", () => {
    // A query like "QX-7741 (a.b)" must not blow up or match everything.
    const segs = highlight("invoice QX-7741 (a.b) here", ["qx-7741", "(a.b)"]);
    expect(marked(segs)).toEqual(["QX-7741", "(a.b)"]);
    expect(text(segs)).toBe("invoice QX-7741 (a.b) here");
  });

  it("returns the text untouched when there are no terms", () => {
    expect(highlight("plain text", [])).toEqual([{ t: "plain text", hit: false }]);
  });

  it("handles empty text", () => {
    expect(text(highlight("", ["x"]))).toBe("");
  });
});

describe("queryTerms", () => {
  it("splits a query into lowercase word tokens", () => {
    expect(queryTerms("Lease Renewal, 2026!")).toEqual(["lease", "renewal", "2026"]);
  });

  it("returns nothing for a query with no words", () => {
    expect(queryTerms("   ??  ")).toEqual([]);
  });
});
