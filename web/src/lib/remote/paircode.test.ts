// Locks the pairing-code crypto to app/smartbrain_3000/pairing_code.py: if the PBKDF2
// derivation or the MAC construction drifts between the two, pairing-by-code silently fails
// (different broker room / different key). The reference vectors below were produced by the
// Python module for the code "ABC234".

import { describe, expect, it } from "vitest";

import { deriveCode, mac, normalizeCode } from "./paircode";

const hex = (b: Uint8Array) => [...b].map((x) => x.toString(16).padStart(2, "0")).join("");

describe("paircode crypto is byte-identical to pairing_code.py", () => {
  it("derives the same broker room as Python", async () => {
    const { roomId } = await deriveCode("ABC234");
    expect(roomId).toBe("sbpair-9f02951c6a1a26e5b6e6da5fe70b58dd");
  });

  it("produces the same MAC as Python (validates the derived key + construction)", async () => {
    const { codeKey } = await deriveCode("ABC234");
    const m = await mac(codeKey, "host", new Uint8Array(16), new Uint8Array(32));
    expect(hex(m)).toBe("26bbc6f56eb0c6a20525699e19bbaed12e7e1b9162d72ad14f2da0222d60e056");
  });

  it("normalizes input the same way (uppercase, drop non-alphabet)", () => {
    expect(normalizeCode("abc 234")).toBe("ABC234");
    expect(normalizeCode("a-b-c-2-3-4")).toBe("ABC234");
  });

  it("binds the MAC to the label (host != guest)", async () => {
    const { codeKey } = await deriveCode("ABC234");
    const h = await mac(codeKey, "host", new Uint8Array(16), new Uint8Array(32));
    const g = await mac(codeKey, "guest", new Uint8Array(16), new Uint8Array(32));
    expect(hex(h)).not.toBe(hex(g));
  });

  it("rejects a code that doesn't normalize to 6 characters (front-line guard)", async () => {
    await expect(deriveCode("ABC")).rejects.toThrow();
    await expect(deriveCode("ABCDEFG")).rejects.toThrow();
    await expect(deriveCode("")).rejects.toThrow();
    // Characters outside the alphabet are filtered out — "ABC23" becomes 5 chars after
    // filtering, which is too short.
    await expect(deriveCode("ABC-23!")).rejects.toThrow();
  });

  it("derives a DIFFERENT room for a different code (each code = its own broker room)", async () => {
    const a = await deriveCode("ABC234");
    const b = await deriveCode("ABC235");
    expect(a.roomId).not.toBe(b.roomId);
  });
});
