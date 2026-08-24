// AudioWorklet processor for push-to-talk capture (see lib/audio/recorder.ts).
// A real same-origin file, NOT an inline blob: URL — the app's CSP (script-src 'self')
// rightly refuses blob: scripts, and the recorder must load under that policy.
registerProcessor(
  "sb-capture",
  class extends AudioWorkletProcessor {
    process(inputs) {
      const ch = inputs[0] && inputs[0][0];
      if (ch && ch.length) this.port.postMessage(ch.slice(0));
      return true;
    }
  },
);
