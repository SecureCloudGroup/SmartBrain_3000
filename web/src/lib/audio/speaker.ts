// Spoken output with a fallback chain: browser system voices (instant, offline,
// excellent on macOS/Windows/iOS/Android) -> the configured server's /v1/audio/speech
// (mainly Linux desktops, whose browsers often ship NO voices) -> a caller-visible
// "unavailable" so the UI can say so honestly instead of going silently mute.

import { sentenceChunks, speakableText } from "./chunker";

/** Resolve the system voice list, surviving the async voiceschanged dance. */
export function voicesReady(timeoutMs = 700): Promise<SpeechSynthesisVoice[]> {
  return new Promise((resolve) => {
    if (typeof speechSynthesis === "undefined") return resolve([]);
    const now = speechSynthesis.getVoices();
    if (now.length) return resolve(now);
    const timer = setTimeout(() => resolve(speechSynthesis.getVoices()), timeoutMs);
    speechSynthesis.addEventListener(
      "voiceschanged",
      () => {
        clearTimeout(timer);
        resolve(speechSynthesis.getVoices());
      },
      { once: true },
    );
  });
}

/** Fetches one spoken chunk from the server (api.voiceSpeak); null = no server voice. */
export type ServerSpeech = (text: string) => Promise<Blob | null>;

export class Speaker {
  private queue: string[] = [];
  private buffer = "";
  private playing = false;
  private stopped = false;
  private audioEl: HTMLAudioElement | null = null;
  private useSystem: boolean | null = null; // resolved on first speak
  constructor(
    private serverSpeech: ServerSpeech | null = null,
    private onIdle: (() => void) | null = null, // fires when the queue drains (UI label reset)
  ) {}

  /** Mirrors `speaking` into Svelte state: class fields are invisible to the reactivity
      graph, and the composer needs a Stop button the whole time a reply is being read. */
  onSpeaking: ((on: boolean) => void) | null = null;

  get speaking(): boolean {
    return this.playing || this.queue.length > 0;
  }

  /** Feed streamed markdown; complete sentences start speaking immediately. */
  feed(markdown: string): void {
    this.buffer += markdown;
    const { chunks, rest } = sentenceChunks(this.buffer);
    this.buffer = rest;
    for (const c of chunks) this.say(c);
  }

  /** Speak the unfinished remainder (call when the stream completes). */
  flush(): void {
    const tail = this.buffer.trim();
    this.buffer = "";
    if (tail) this.say(tail);
  }

  /** Speak one complete text (the per-message Listen button). */
  say(text: string): void {
    const clean = speakableText(text);
    if (!clean) return;
    this.stopped = false;
    this.queue.push(clean);
    void this.pump();
  }

  /** Barge-in: stop everything now and drop the queue. */
  stop(): void {
    this.stopped = true;
    this.queue = [];
    this.buffer = "";
    if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
    if (this.audioEl) {
      this.audioEl.pause();
      this.audioEl = null;
    }
    this.playing = false;
    this.onSpeaking?.(false);
  }

  private async pump(): Promise<void> {
    if (this.playing) return;
    const next = this.queue.shift();
    if (next === undefined) return;
    this.playing = true;
    this.onSpeaking?.(true);
    try {
      if (this.useSystem === null) this.useSystem = (await voicesReady()).length > 0;
      if (this.stopped) return;
      if (this.useSystem) await this.speakSystem(next);
      else if (this.serverSpeech) await this.speakServer(next);
      // neither available: swallow — the UI gates the toggle on availability
    } finally {
      this.playing = false;
      if (!this.stopped && this.queue.length > 0) void this.pump();
      else {
        this.onSpeaking?.(false);
        if (!this.stopped && this.queue.length === 0) this.onIdle?.();
      }
    }
  }

  private speakSystem(text: string): Promise<void> {
    return new Promise((resolve) => {
      const u = new SpeechSynthesisUtterance(text);
      u.rate = speechRate(); // Settings → Status → Playback speed (system voices only)
      u.onend = () => resolve();
      u.onerror = () => resolve(); // a bad utterance must not wedge the queue
      speechSynthesis.speak(u);
    });
  }

  private async speakServer(text: string): Promise<void> {
    const blob = await this.serverSpeech?.(text).catch(() => null);
    if (!blob || this.stopped) return;
    await new Promise<void>((resolve) => {
      const el = new Audio(URL.createObjectURL(blob));
      this.audioEl = el;
      el.onended = () => {
        URL.revokeObjectURL(el.src);
        resolve();
      };
      el.onerror = () => resolve();
      void el.play().catch(() => resolve());
    });
    this.audioEl = null;
  }
}

/** Playback speed for spoken replies, persisted per device. 1 = natural; system voices
 * accept 0.5–2 sensibly. Read at speak time so a change applies to the next sentence. */
export const SPEECH_RATE_KEY = "sb:tts-rate";
export function speechRate(): number {
  try {
    const v = parseFloat(localStorage.getItem(SPEECH_RATE_KEY) ?? "1");
    return Number.isFinite(v) && v >= 0.5 && v <= 2 ? v : 1;
  } catch {
    return 1;
  }
}

/** True when ANY spoken output can work: system voices or a configured server voice. */
export async function speechAvailable(serverTts: boolean): Promise<boolean> {
  return serverTts || (await voicesReady()).length > 0;
}
