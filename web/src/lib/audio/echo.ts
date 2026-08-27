// Self-echo detection: the microphone hears the reply being read aloud.
//
// getUserMedia's echoCancellation only cancels audio the BROWSER plays through WebRTC —
// not the operating system's speech voice — so on a laptop, and far worse on a phone
// whose speaker sits beside its mic, the spoken reply comes back as a "dictation" and
// hands-free sends the fragments: a loop. A level threshold cannot tell the speaker
// from the user. The transcript can: an echo is a run of the reply's own words.

import { normalizeWake } from "./wakeword";

/**
 * True when `heard` is mostly made of words that appear IN ORDER in `spoken` — the
 * reply's own text coming back through the mic. Short, common phrases ("yes", "ok")
 * never count: at least four words are needed, and 70 % of them must match as an
 * ordered subsequence with small gaps.
 */
export function looksLikeEcho(heard: string, spoken: string): boolean {
  const h = normalizeWake(heard).split(" ").filter(Boolean);
  const s = normalizeWake(spoken).split(" ").filter(Boolean);
  if (h.length < 4 || s.length === 0) return false;
  let matched = 0;
  let j = 0;
  for (const w of h) {
    // find w in s at or after j, within a short window (dropped/misheard words allowed)
    let k = -1;
    for (let t = j; t < Math.min(s.length, j + 6); t++) {
      if (s[t] === w) {
        k = t;
        break;
      }
    }
    if (k === -1) {
      // not near: allow one resync anywhere later in the text
      const anywhere = s.indexOf(w, j);
      if (anywhere !== -1 && matched === 0) k = anywhere;
    }
    if (k !== -1) {
      matched++;
      j = k + 1;
    }
  }
  return matched / h.length >= 0.7;
}
