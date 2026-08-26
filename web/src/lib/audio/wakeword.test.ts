import { describe, expect, it } from "vitest";
import { isStopListening, matchWake, normalizeWake } from "./wakeword";

describe("matchWake", () => {
  it("hits on the exact phrase and hands back the rest of the utterance", () => {
    const m = matchWake("Hey SmartBrain, what's on my calendar?", "Hey SmartBrain");
    expect(m.hit).toBe(true);
    expect(m.rest).toBe("what s on my calendar");
  });
  it("tolerates the engine's spelling of a name (one edit per five characters)", () => {
    expect(matchWake("Hey Merle", "Hey Merl").hit).toBe(true);
    expect(matchWake("Hey Katherine", "Hey Catherine").hit).toBe(true);
  });
  it("refuses a different name and a phrase buried mid-sentence", () => {
    expect(matchWake("Hey Google", "Hey SmartBrain").hit).toBe(false);
    expect(matchWake("I said hey SmartBrain earlier", "Hey SmartBrain").hit).toBe(false);
  });
  it("accepts a learned alias the test recorded", () => {
    expect(matchWake("Hey Mural, lights", "Hey Merl", ["hey mural"]).hit).toBe(true);
    expect(matchWake("Hey Mural, lights", "Hey Merl").hit).toBe(false);
  });
  it("is empty-safe", () => {
    expect(matchWake("", "Hey Merl").hit).toBe(false);
    expect(matchWake("Hey Merl", "").hit).toBe(false);
  });
});

describe("isStopListening / normalizeWake", () => {
  it("recognises whole-utterance exits only", () => {
    expect(isStopListening("Stop listening.")).toBe(true);
    expect(isStopListening("goodbye")).toBe(true);
    expect(isStopListening("please stop the server")).toBe(false);
    expect(isStopListening("stop")).toBe(false); // a fragment, not an exit
  });
  it("normalizes punctuation and case", () => {
    expect(normalizeWake("  Hey, MERL!  ")).toBe("hey merl");
  });
});
