// The appbar shows the running version under the logo, but only when health actually returns one —
// otherwise there must be no broken "v". displayVersion is the pure choke-point for that rule.

import { describe, expect, it } from "vitest";

import { displayVersion } from "./version";

describe("displayVersion", () => {
  it("prefixes a real version with v", () => {
    expect(displayVersion("0.5.1")).toBe("v0.5.1");
    expect(displayVersion("0.0.0-dev")).toBe("v0.0.0-dev");
  });

  it("renders nothing when the version is empty or missing", () => {
    expect(displayVersion("")).toBe("");
    expect(displayVersion(undefined)).toBe("");
    expect(displayVersion(null)).toBe("");
  });
});
