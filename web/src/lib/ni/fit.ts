// Pure helpers behind the visible fit advisory (operator ruling 2026-10-12): a card whose design
// may not answer the ask wears a "May not answer your question" chip (CardShell) and the C2 panel
// says so in one line (VerifyPanel). Advisory only — nothing here blocks or hides anything. The
// board row's `fit` is code-built (NiFit: a verdict and at most 3 code-written labels) and every
// sentence below is code-authored. A row without it, or with a shape this client does not know
// (an older or newer server), reads as "nothing to show" — never a throw (the version-skew rule).

import type { NiFit } from "$lib/api";

/** The most labels a card names (the server sends at most 3; a longer list is cut here). */
export const FIT_MAX_MISSING = 3;

/** What CardShell shows: the chip's words, and "Missing: a, b, c" (null when nothing is named). */
export interface FitText {
  chip: string;
  detail: string | null;
}

const CHIP: Readonly<Record<NiFit["verdict"], string>> = {
  no: "May not answer your question",
  partly: "May not fully answer your question",
};

const LINE: Readonly<Record<NiFit["verdict"], string>> = {
  no: "This card may not answer your question.",
  partly: "This card may not fully answer your question.",
};

/** The row's `fit` when it has the known shape — a "no"/"partly" verdict and a `missing` list —
 *  keeping the non-blank string labels (trimmed), cut to FIT_MAX_MISSING; else null. */
function readFit(raw: unknown): NiFit | null {
  console.assert(FIT_MAX_MISSING > 0, "readFit: a positive label cap");
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const { verdict, missing } = raw as { verdict?: unknown; missing?: unknown };
  if ((verdict !== "no" && verdict !== "partly") || !Array.isArray(missing)) return null;
  const list: readonly unknown[] = missing;
  const labels = list
    .filter((label): label is string => typeof label === "string" && label.trim() !== "")
    .map((label) => label.trim())
    .slice(0, FIT_MAX_MISSING);
  console.assert(labels.length <= FIT_MAX_MISSING, "readFit: at most FIT_MAX_MISSING labels");
  return { verdict, missing: labels };
}

/** The card's chip words for the row's `fit`, and its detail line ("Missing: a, b, c" — the
 *  chip's title, and the footer's second line on a phone); null when there is nothing to show. */
export function fitText(raw: unknown): FitText | null {
  const fit = readFit(raw);
  if (!fit) return null;
  const detail = fit.missing.length > 0 ? `Missing: ${fit.missing.join(", ")}` : null;
  console.assert(CHIP[fit.verdict].length > 0, "fitText: a chip for every verdict");
  console.assert(detail === null || detail.startsWith("Missing: "), "fitText: the detail names what is missing");
  return { chip: CHIP[fit.verdict], detail };
}

/** VerifyPanel's (C2) line for the row's `fit`: "This card may not (fully) answer your question.",
 *  then " It lacks: a, b." when labels are named; "" when there is nothing to show. */
export function fitLine(raw: unknown): string {
  const fit = readFit(raw);
  if (!fit) return "";
  const lacks = fit.missing.length > 0 ? ` It lacks: ${fit.missing.join(", ")}.` : "";
  console.assert(LINE[fit.verdict].endsWith("."), "fitLine: a whole sentence for every verdict");
  console.assert(lacks === "" || lacks.endsWith("."), "fitLine: the lacks clause is a sentence");
  return `${LINE[fit.verdict]}${lacks}`;
}
