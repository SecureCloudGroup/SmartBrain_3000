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
  private peakLevel = 0;
  /** Live input level (0..1-ish RMS per chunk) — the UI animates it so "is it hearing
      me?" is never a mystery. Assigned by the caller before start(). */
  onLevel: ((level: number) => void) | null = null;

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
    // Safari creates contexts SUSPENDED even inside a user gesture; without this the
    // graph runs but processes zero samples — a red recording button capturing pure
    // silence, which the field hit as "prompt appeared, button red, no text".
    await this.context.resume();
    const source = this.context.createMediaStreamSource(this.stream);
    const push = (chunk: Float32Array) => {
      this.parts.push(chunk);
      this.length += chunk.length;
      let sum = 0;
      for (let i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
      const rms = Math.sqrt(sum / chunk.length);
      if (rms > this.peakLevel) this.peakLevel = rms;
      this.onLevel?.(rms);
    };
    try {
      await this.context.audioWorklet.addModule("/capture-worklet.js");
      const node = new AudioWorkletNode(this.context, "sb-capture");
      node.port.onmessage = (e: MessageEvent<Float32Array>) => push(e.data);
      source.connect(node);
      // Through a zero-gain sink to the destination: rendering graphs only reliably
      // PULL nodes on a path to a destination — a dangling worklet can process nothing
      // at all (measured: level 0.00 in Chromium). The mute gain keeps it inaudible.
      const sink = this.context.createGain();
      sink.gain.value = 0;
      node.connect(sink);
      sink.connect(this.context.destination);
      this.node = node;
      this.silentSink = sink;
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

  /** Stop capturing; returns the WAV plus what was actually heard, so the caller can
      say "that was silence" instead of transcribing nothing and showing nothing. */
  async stop(): Promise<{ blob: Blob; seconds: number; peak: number }> {
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
    const peak = this.peakLevel;
    this.peakLevel = 0;
    const samples = downsample(all, rate, TARGET_RATE);
    return {
      blob: new Blob([encodeWav(samples, TARGET_RATE)], { type: "audio/wav" }),
      seconds: samples.length / TARGET_RATE,
      peak,
    };
  }
}
