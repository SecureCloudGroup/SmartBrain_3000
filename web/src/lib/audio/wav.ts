// 16 kHz mono 16-bit WAV, encoded client-side. Browsers record different codecs by
// default (Safari→AAC, Chrome→Opus) and whether a local whisper server can decode them
// depends on its ffmpeg situation — a cross-platform landmine. WAV is accepted by every
// engine identically, and a push-to-talk utterance at 16 kHz mono is ~32 KB/s, trivial
// even over the phone's WebRTC bridge.

export const TARGET_RATE = 16000;

/** Linear-interpolation downsample to `toRate`. Pass-through when rates match. */
export function downsample(samples: Float32Array, fromRate: number, toRate: number): Float32Array {
  console.assert(fromRate > 0 && toRate > 0, "sample rates must be positive");
  if (fromRate === toRate) return samples;
  const ratio = fromRate / toRate;
  const out = new Float32Array(Math.floor(samples.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const pos = i * ratio;
    const left = Math.floor(pos);
    const right = Math.min(left + 1, samples.length - 1);
    const frac = pos - left;
    out[i] = samples[left] * (1 - frac) + samples[right] * frac;
  }
  return out;
}

/** Drop the OLDEST chunks until at most `maxSamples` remain — standby listening keeps a
    short rolling window instead of growing without bound while nothing is said. */
export function trimParts(parts: Float32Array[], maxSamples: number): Float32Array[] {
  let total = 0;
  for (const p of parts) total += p.length;
  let i = 0;
  while (i < parts.length && total - parts[i].length >= maxSamples) {
    total -= parts[i].length;
    i++;
  }
  return i === 0 ? parts : parts.slice(i);
}

/** Concatenate captured chunks and produce a 16 kHz mono WAV — the ONE path both the
    final recording and the live (mid-utterance) snapshots go through. */
export function partsToWav(parts: Float32Array[], fromRate: number): { blob: Blob; seconds: number } {
  let length = 0;
  for (const p of parts) length += p.length;
  const all = new Float32Array(length);
  let off = 0;
  for (const p of parts) {
    all.set(p, off);
    off += p.length;
  }
  const samples = downsample(all, fromRate, TARGET_RATE);
  return { blob: new Blob([encodeWav(samples, TARGET_RATE)], { type: "audio/wav" }), seconds: samples.length / TARGET_RATE };
}

/** Encode mono float samples ([-1, 1]) as a 16-bit PCM WAV file. */
export function encodeWav(samples: Float32Array, sampleRate: number): ArrayBuffer {
  console.assert(sampleRate > 0, "sample rate must be positive");
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buf);
  const writeStr = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true); // fmt chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate (16-bit mono)
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  writeStr(36, "data");
  view.setUint32(40, samples.length * 2, true);
  let off = 44;
  for (let i = 0; i < samples.length; i++, off += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buf;
}
