import { describe, expect, it } from "vitest";
import { encodeRequestParts, REQUEST_CHUNK_CHARS } from "./protocol";

describe("encodeRequestParts", () => {
  it("keeps small bodies as the single legacy frame", () => {
    const frames = encodeRequestParts("7", "POST", "/api/x", {}, new Uint8Array(100));
    expect(frames).toHaveLength(1);
    expect(JSON.parse(frames[0]).rq).toBeUndefined();
  });
  it("splits a dictation-sized body into ordered parts with the head on seq 0", () => {
    const body = new Uint8Array(300 * 1024); // ~400 KB base64: the field failure size
    const frames = encodeRequestParts("8", "POST", "/api/voice/transcribe", { "content-type": "audio/wav" }, body);
    expect(frames.length).toBeGreaterThan(1);
    const head = JSON.parse(frames[0]);
    expect(head).toMatchObject({ rq: true, id: "8", seq: 0, more: true, method: "POST", path: "/api/voice/transcribe" });
    const last = JSON.parse(frames[frames.length - 1]);
    expect(last.more).toBe(false);
    expect(last.method).toBeUndefined(); // only the head carries routing
    for (const f of frames) expect(f.length).toBeLessThan(REQUEST_CHUNK_CHARS + 512);
  });
});
