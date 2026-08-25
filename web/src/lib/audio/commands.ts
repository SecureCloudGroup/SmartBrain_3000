// Spoken control commands, Dragon/Apple-dictation style: a trailing "send" submits,
// a lone "cancel" discards, "start over" clears and re-listens. Deliberately tiny —
// Whisper already punctuates, so dictation grammar beyond actions isn't worth its
// false-trigger rate.

export type VoiceAction = "send" | "cancel" | "restart" | null;

const TRAILING = /[\s.,!?;:]+$/;

/** Split a transcript into (action, remaining text). Commands only count as commands
 * when they END the utterance (or are the whole of it) — "cancel my dentist
 * appointment tomorrow" must stay ordinary text. */
export function parseVoiceCommand(raw: string): { action: VoiceAction; text: string } {
  const text = raw.trim();
  const bare = text.replace(TRAILING, "").toLowerCase();
  // Whole-utterance commands
  if (bare === "cancel" || bare === "scratch that" || bare === "never mind") {
    return { action: "cancel", text: "" };
  }
  if (bare === "start over" || bare === "start again") {
    return { action: "restart", text: "" };
  }
  if (bare === "send" || bare === "send it" || bare === "send message") {
    return { action: "send", text: "" };
  }
  // Trailing "… send" / "… send it"
  const m = bare.match(/^(.*?)[\s,]+send(?:\s+it|\s+message)?$/);
  if (m && m[1].trim()) {
    // Reconstruct the original casing/punctuation minus the command tail: cut the raw
    // string at the last occurrence of the command words.
    const cut = text.toLowerCase().lastIndexOf("send");
    const kept = text.slice(0, cut).replace(TRAILING, "");
    return { action: "send", text: kept };
  }
  return { action: null, text };
}
