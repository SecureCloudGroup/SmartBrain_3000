import { describe, expect, it } from "vitest";
import { FIT_MAX_MISSING, fitLine, fitText } from "./fit";

describe("fitText", () => {
  it("returns null for absent, null, or a non-object shape", () => {
    expect(fitText(undefined)).toBeNull();
    expect(fitText(null)).toBeNull();
    expect(fitText("no")).toBeNull();
    expect(fitText(42)).toBeNull();
    expect(fitText([])).toBeNull();
  });

  it("returns null for an unknown verdict or a missing/non-array missing field", () => {
    expect(fitText({ verdict: "yes", missing: [] })).toBeNull();
    expect(fitText({ verdict: "no" })).toBeNull();
    expect(fitText({ verdict: "no", missing: "wind" })).toBeNull();
  });

  it("builds the 'no' chip with no detail when nothing is missing", () => {
    expect(fitText({ verdict: "no", missing: [] })).toEqual({
      chip: "May not answer your question",
      detail: null,
    });
  });

  it("builds the 'partly' chip with the Missing: detail", () => {
    expect(fitText({ verdict: "partly", missing: ["temperature", "wind"] })).toEqual({
      chip: "May not fully answer your question",
      detail: "Missing: temperature, wind",
    });
  });

  it("trims whitespace, drops blank/non-string entries", () => {
    expect(fitText({ verdict: "no", missing: [" wind ", "", "  ", 7, null] })).toEqual({
      chip: "May not answer your question",
      detail: "Missing: wind",
    });
  });

  it("reads to null detail when every entry is dropped", () => {
    expect(fitText({ verdict: "no", missing: ["", "  "] })?.detail).toBeNull();
  });

  it(`cuts missing to ${FIT_MAX_MISSING} labels`, () => {
    const fit = fitText({ verdict: "no", missing: ["a", "b", "c", "d", "e"] });
    expect(fit?.detail).toBe("Missing: a, b, c");
  });
});

describe("fitLine", () => {
  it("returns \"\" for absent, null, or a garbage shape", () => {
    expect(fitLine(undefined)).toBe("");
    expect(fitLine(null)).toBe("");
    expect(fitLine({ verdict: "no" })).toBe("");
    expect(fitLine({ verdict: "no", missing: "wind" })).toBe("");
  });

  it("says the card may not answer, with no lacks clause when nothing is missing", () => {
    expect(fitLine({ verdict: "no", missing: [] })).toBe("This card may not answer your question.");
  });

  it("says the card may not fully answer, and names what it lacks", () => {
    expect(fitLine({ verdict: "partly", missing: ["temperature", "wind"] })).toBe(
      "This card may not fully answer your question. It lacks: temperature, wind.",
    );
  });

  it(`cuts the lacks clause to ${FIT_MAX_MISSING} labels`, () => {
    expect(fitLine({ verdict: "no", missing: ["a", "b", "c", "d"] })).toBe(
      "This card may not answer your question. It lacks: a, b, c.",
    );
  });
});
