import { describe, expect, it } from "vitest";
import { downsample, encodeWav } from "./wav";

describe("downsample", () => {
  it("passes through at equal rates", () => {
    const s = new Float32Array([0.1, 0.2, 0.3]);
    expect(downsample(s, 16000, 16000)).toBe(s);
  });
  it("halves length at 2x rate and interpolates", () => {
    const s = new Float32Array([0, 1, 0, 1, 0, 1, 0, 1]);
    const out = downsample(s, 32000, 16000);
    expect(out.length).toBe(4);
    expect(out[0]).toBe(0); // exact sample positions
    expect(out[1]).toBe(0);
  });
  it("48k -> 16k gives one third the samples", () => {
    const out = downsample(new Float32Array(48000), 48000, 16000);
    expect(out.length).toBe(16000);
  });
});

describe("encodeWav", () => {
  it("writes a valid RIFF/WAVE header for 16kHz mono 16-bit", () => {
    const buf = encodeWav(new Float32Array([0, 0.5, -0.5, 1]), 16000);
    const view = new DataView(buf);
    const tag = (off: number, len: number) =>
      String.fromCharCode(...new Uint8Array(buf, off, len));
    expect(tag(0, 4)).toBe("RIFF");
    expect(tag(8, 4)).toBe("WAVE");
    expect(view.getUint16(20, true)).toBe(1); // PCM
    expect(view.getUint16(22, true)).toBe(1); // mono
    expect(view.getUint32(24, true)).toBe(16000);
    expect(view.getUint16(34, true)).toBe(16); // bits
    expect(view.getUint32(40, true)).toBe(8); // 4 samples * 2 bytes
    expect(buf.byteLength).toBe(44 + 8);
  });
  it("clamps out-of-range samples instead of wrapping", () => {
    const buf = encodeWav(new Float32Array([2, -2]), 16000);
    const view = new DataView(buf);
    expect(view.getInt16(44, true)).toBe(0x7fff);
    expect(view.getInt16(46, true)).toBe(-0x8000);
  });
});
