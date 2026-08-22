import { describe, expect, it } from "vitest";
import { resumeUpdateAction } from "./update";

describe("resumeUpdateAction", () => {
  it("does nothing when versions match or are unknown", () => {
    expect(resumeUpdateAction("0.9.7", "0.9.7", false)).toBe("none");
    expect(resumeUpdateAction("", "0.9.7", false)).toBe("none");
    expect(resumeUpdateAction("0.9.7", "", true)).toBe("none");
  });

  it("reloads automatically when locked — the post-update state, nothing to lose", () => {
    expect(resumeUpdateAction("0.9.6", "0.9.7", false)).toBe("reload");
  });

  it("offers the banner when unlocked — never reload over someone's work", () => {
    expect(resumeUpdateAction("0.9.6", "0.9.7", true)).toBe("banner");
  });
});
