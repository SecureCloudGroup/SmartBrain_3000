// User-defined wake word ("Hey Merl", "Hey Catherine", "Hey SmartBrain") on top of the
// speech engine we already ship: standby listens with VAD only, and when speech is heard
// a fast partial transcription is checked for the phrase at the START of the utterance.
//
// Whisper spells unusual names its own way ("Hey Merl" can come back "Hey Merle"), so the
// Settings test records what the engine actually heard and stores those spellings as
// ALIASES — that is what makes an arbitrary name work naturally instead of by luck.

export const WAKE_WORD_KEY = "sb:wakeword";
export const WAKE_ALIASES_KEY = "sb:wakeword-aliases";
export const CONVERSATION_KEY = "sb:conversation";

/** Lower-case, letters/digits/spaces only, single-spaced — the comparison form. */
export function normalizeWake(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function levenshtein(a: string, b: string): number {
  const prev = new Array<number>(b.length + 1);
  for (let j = 0; j <= b.length; j++) prev[j] = j;
  for (let i = 1; i <= a.length; i++) {
    let diag = prev[0];
    prev[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const tmp = prev[j];
      prev[j] = Math.min(prev[j] + 1, prev[j - 1] + 1, diag + (a[i - 1] === b[j - 1] ? 0 : 1));
      diag = tmp;
    }
  }
  return prev[b.length];
}

export type WakeMatch = { hit: boolean; rest: string; heard: string };

/**
 * Does the transcript START with the wake phrase (or one of its learned aliases)?
 * Tolerates small spelling drift (≤ 1 edit per 5 characters), compares against the
 * same number of words as the phrase, and returns whatever followed the phrase so
 * "Hey Merl, what's the weather" carries the question through in one breath.
 */
export function matchWake(transcript: string, phrase: string, aliases: string[] = []): WakeMatch {
  const heard = normalizeWake(transcript);
  const words = heard.split(" ").filter(Boolean);
  for (const cand of [phrase, ...aliases].map(normalizeWake).filter(Boolean)) {
    const n = cand.split(" ").length;
    const head = words.slice(0, n).join(" ");
    if (!head) continue;
    const budget = Math.floor(cand.length / 5);
    if (levenshtein(head, cand) <= budget) {
      return { hit: true, rest: words.slice(n).join(" "), heard };
    }
  }
  return { hit: false, rest: "", heard };
}

/** Spoken exits from conversation mode — a whole short utterance, not a mid-sentence word. */
export function isStopListening(text: string): boolean {
  const t = normalizeWake(text);
  return /^(stop listening|stop|goodbye|good bye|bye|that s all|thats all|go to sleep)( now)?$/.test(t);
}

export function loadWakeWord(): { phrase: string; aliases: string[] } {
  try {
    const phrase = localStorage.getItem(WAKE_WORD_KEY) ?? "";
    const raw = localStorage.getItem(WAKE_ALIASES_KEY);
    const aliases = raw ? (JSON.parse(raw) as string[]) : [];
    return { phrase, aliases: Array.isArray(aliases) ? aliases : [] };
  } catch {
    return { phrase: "", aliases: [] };
  }
}

export function saveWakeWord(phrase: string, aliases: string[]): void {
  try {
    if (phrase.trim()) {
      localStorage.setItem(WAKE_WORD_KEY, phrase.trim());
      localStorage.setItem(WAKE_ALIASES_KEY, JSON.stringify(aliases));
    } else {
      localStorage.removeItem(WAKE_WORD_KEY);
      localStorage.removeItem(WAKE_ALIASES_KEY);
    }
  } catch {
    /* storage unavailable — session-only */
  }
}
