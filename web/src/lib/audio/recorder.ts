// Push-to-talk microphone capture -> 16 kHz mono WAV blob.
//
// Captures at the context's NATIVE rate via an AudioWorklet (Safari quietly ignores a
// requested sampleRate, so asking for 16 kHz is a lie on the platform we care most
// about) and downsamples in JS — the one path that behaves identically on Safari,
// Chrome, and Firefox, desktop and phone. The worklet module ships inline as a blob
// URL so there's no extra asset to route.

import { downsample, encodeWav, TARGET_RATE } from "./wav";

const WORKLET_SRC = `
registerProcessor("sb-capture", class extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) this.port.postMessage(ch.slice(0));
    return true;
  }
});
`;

export class Recorder {
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private parts: Float32Array[] = [];
  private length = 0;

  get active(): boolean {
    return this.context !== null;
  }

  /** Ask for the mic and start capturing. Throws (with the browser's reason) on denial. */
  async start(): Promise<void> {
    console.assert(!this.context, "recorder already running");
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
    this.context = new AudioContext();
    const url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "text/javascript" }));
    try {
      await this.context.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    this.node = new AudioWorkletNode(this.context, "sb-capture");
    this.node.port.onmessage = (e: MessageEvent<Float32Array>) => {
      this.parts.push(e.data);
      this.length += e.data.length;
    };
    this.context.createMediaStreamSource(this.stream).connect(this.node);
    // NOT connected to destination: capture only, no monitoring feedback loop.
  }

  /** Stop capturing and return the WAV blob (empty recording -> zero-sample WAV). */
  async stop(): Promise<Blob> {
    const rate = this.context?.sampleRate ?? TARGET_RATE;
    this.node?.port.close();
    this.node?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    await this.context?.close().catch(() => undefined);
    this.node = null;
    this.stream = null;
    this.context = null;
    const all = new Float32Array(this.length);
    let off = 0;
    for (const p of this.parts) {
      all.set(p, off);
      off += p.length;
    }
    this.parts = [];
    this.length = 0;
    return new Blob([encodeWav(downsample(all, rate, TARGET_RATE), TARGET_RATE)], { type: "audio/wav" });
  }
}
