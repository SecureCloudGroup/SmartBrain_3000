import { describe, expect, it } from "vitest";
import { looksLikeEcho } from "./echo";

const reply = "You have eight active development tasks, all ongoing with no hard deadlines today. Prioritize based on your current sprint goals.";

describe("looksLikeEcho", () => {
  it("catches the reply coming back through the mic, even partially and misheard", () => {
    expect(looksLikeEcho("eight active development tasks all ongoing with no hard", reply)).toBe(true);
    expect(looksLikeEcho("prioritize based on your current sprint goals", reply)).toBe(true);
    expect(looksLikeEcho("active development tasks all going with no hard deadlines", reply)).toBe(true); // "going" misheard
  });
  it("lets a real follow-up through, even one that reuses a few words", () => {
    expect(looksLikeEcho("which of those tasks has the nearest deadline", reply)).toBe(false);
    expect(looksLikeEcho("what about the CSS fix", reply)).toBe(false);
  });
  it("never flags short utterances or when nothing was spoken", () => {
    expect(looksLikeEcho("yes", reply)).toBe(false);
    expect(looksLikeEcho("no hard deadlines today", "")).toBe(false);
  });
});
