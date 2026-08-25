import { describe, expect, it } from "vitest";
import { parseVoiceCommand } from "./commands";

describe("parseVoiceCommand", () => {
  it("whole-utterance commands", () => {
    expect(parseVoiceCommand("Cancel.")).toEqual({ action: "cancel", text: "" });
    expect(parseVoiceCommand("scratch that")).toEqual({ action: "cancel", text: "" });
    expect(parseVoiceCommand("Start over!")).toEqual({ action: "restart", text: "" });
    expect(parseVoiceCommand("Send it")).toEqual({ action: "send", text: "" });
  });
  it("trailing send strips the command and keeps the message", () => {
    const r = parseVoiceCommand("Add milk to my tasks. Send it.");
    expect(r.action).toBe("send");
    expect(r.text).toBe("Add milk to my tasks");
  });
  it("mid-sentence command words stay ordinary text", () => {
    const r = parseVoiceCommand("Cancel my dentist appointment tomorrow");
    expect(r.action).toBeNull();
    expect(r.text).toBe("Cancel my dentist appointment tomorrow");
    expect(parseVoiceCommand("I need to send the report by five").action).toBeNull();
  });
  it("plain text passes through untouched", () => {
    expect(parseVoiceCommand("  The dog barked loud.  ")).toEqual({ action: null, text: "The dog barked loud." });
  });
});
