// Push-to-talk microphone capture -> 16 kHz mono WAV blob.
//
// Captures at the context's NATIVE rate via an AudioWorklet (Safari quietly ignores a
// requested sampleRate, so asking for 16 kHz is a lie on the platform we care most
// about) and downsamples in JS — the one path that behaves identically on Safari,
// Chrome, and Firefox, desktop and phone. The worklet ships as a real static file
// (/capture-worklet.js): the app's CSP is script-src 'self', which rightly refuses
// an inline blob: module — the first field test failed exactly there.

import { downsample, encodeWav, TARGET_RATE } from "./wav";

export class Recorder {
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private node: AudioWorkletNode | ScriptProcessorNode | null = null;
  private silentSink: GainNode | null = null;
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
    const source = this.context.createMediaStreamSource(this.stream);
    const push = (chunk: Float32Array) => {
      this.parts.push(chunk);
      this.length += chunk.length;
    };
    try {
      await this.context.audioWorklet.addModule("/capture-worklet.js");
      const node = new AudioWorkletNode(this.context, "sb-capture");
      node.port.onmessage = (e: MessageEvent<Float32Array>) => push(e.data);
      source.connect(node);
      this.node = node;
      // NOT connected to destination: capture only, no monitoring feedback loop.
    } catch (err) {
      // The worklet file can 404 on a PWA origin that lags the app by a deploy, or be
      // refused by a stricter CSP — capture must not die over the modern path. The
      // deprecated ScriptProcessor runs everywhere; it needs a sink to fire, so it
      // routes through a zero-gain node (never audible, no feedback).
      console.warn("audio worklet unavailable, using ScriptProcessor fallback:", err);
      const sp = this.context.createScriptProcessor(4096, 1, 1);
      sp.onaudioprocess = (e) => push(new Float32Array(e.inputBuffer.getChannelData(0)));
      const mute = this.context.createGain();
      mute.gain.value = 0;
      source.connect(sp);
      sp.connect(mute);
      mute.connect(this.context.destination);
      this.node = sp;
      this.silentSink = mute;
    }
  }

  /** Stop capturing and return the WAV blob (empty recording -> zero-sample WAV). */
  async stop(): Promise<Blob> {
    const rate = this.context?.sampleRate ?? TARGET_RATE;
    if (this.node instanceof AudioWorkletNode) this.node.port.close();
    this.node?.disconnect();
    this.silentSink?.disconnect();
    this.silentSink = null;
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
