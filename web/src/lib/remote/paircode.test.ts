// Locks the pairing-code crypto to app/smartbrain_3000/pairing_code.py: if the PBKDF2
// derivation or the MAC construction drifts between the two, pairing-by-code silently fails
// (different broker room / different key). The reference vectors below were produced by the
// Python module for the code "ABCD-EFGH" (normalizes to ABCDEFGH).

import { describe, expect, it } from "vitest";

import { deriveCode, mac, normalizeCode } from "./paircode";

const hex = (b: Uint8Array) => [...b].map((x) => x.toString(16).padStart(2, "0")).join("");

describe("paircode crypto is byte-identical to pairing_code.py", () => {
  it("derives the same broker room as Python", async () => {
    const { roomId } = await deriveCode("ABCD-EFGH");
    expect(roomId).toBe("sbpair-566b249c880595cb2fa34a3f97a2a30c");
  });

  it("produces the same MAC as Python (validates the derived key + construction)", async () => {
    const { codeKey } = await deriveCode("ABCD-EFGH");
    const m = await mac(codeKey, "host", new Uint8Array(16), new Uint8Array(32));
    expect(hex(m)).toBe("52d3ce8ee6f6a33154c1e41e20ff76866fe89315eef61f2c1687fd003a0bb4cb");
  });

  it("normalizes input the same way (uppercase, drop non-alphabet)", () => {
    expect(normalizeCode("abcd efgh")).toBe("ABCDEFGH");
    expect(normalizeCode("abcd-efgh")).toBe("ABCDEFGH"); // the display dash is accepted
    expect(normalizeCode("ABCDEFGH")).toBe("ABCDEFGH");
  });

  it("binds the MAC to the label (host != guest)", async () => {
    const { codeKey } = await deriveCode("ABCD-EFGH");
    const h = await mac(codeKey, "host", new Uint8Array(16), new Uint8Array(32));
    const g = await mac(codeKey, "guest", new Uint8Array(16), new Uint8Array(32));
    expect(hex(h)).not.toBe(hex(g));
  });

  it("rejects a code that doesn't normalize to 8 characters (front-line guard)", async () => {
    await expect(deriveCode("ABC234")).rejects.toThrow(); // the old 6-char length
    await expect(deriveCode("ABCDEFGHJ")).rejects.toThrow();
    await expect(deriveCode("")).rejects.toThrow();
    // Characters outside the alphabet are filtered out — "ABCD-EFG!" becomes 7 chars after
    // filtering, which is too short.
    await expect(deriveCode("ABCD-EFG!")).rejects.toThrow();
  });

  it("derives a DIFFERENT room for a different code (each code = its own broker room)", async () => {
    const a = await deriveCode("ABCD-EFGH");
    const b = await deriveCode("ABCD-EFGJ");
    expect(a.roomId).not.toBe(b.roomId);
  });
});
