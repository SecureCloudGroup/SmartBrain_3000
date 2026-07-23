import { describe, expect, it } from "vitest";
import { strToTags, tagsToStr } from "./tags";

describe("strToTags", () => {
  it("splits on commas, trims, and drops blanks", () => {
    expect(strToTags(" property , 2024 ,, taxes ")).toEqual(["property", "2024", "taxes"]);
  });

  it("empty or whitespace-only input means no tags", () => {
    expect(strToTags("")).toEqual([]);
    expect(strToTags("  ,  , ")).toEqual([]);
  });
});

describe("tagsToStr", () => {
  it("joins with a comma-space (what the editor shows)", () => {
    expect(tagsToStr(["property", "2024"])).toBe("property, 2024");
  });

  it("handles missing tags (older rows)", () => {
    expect(tagsToStr(undefined)).toBe("");
    expect(tagsToStr([])).toBe("");
  });

  it("roundtrips through the editor unchanged", () => {
    const tags = ["a", "b c", "d"];
    expect(strToTags(tagsToStr(tags))).toEqual(tags);
  });
});
