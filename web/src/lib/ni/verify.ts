// Pure option-list logic behind VerifyPanel.svelte (ni-format §34 C2 presentation_id,
// Phase 1a-4). A bound form node's sealed `design` carries only `{designer, pick}` on
// the board today (ni.py `_bind_form`) — the runner-up's own CLIR never reaches the
// client, even though `POST /validate` already accepts `presentation_id: "second"` and
// 409s if none was sealed. The plan names `alternatives: [{id, form, clir, summary}]`
// (≤1 entry) as the payload shape a parallel engine change adds so the client can show
// it; it is absent today, so this reads it defensively and degrades to one option.

export interface VerifyOption {
  id: "pick" | "second";
  clir: { desktop: unknown; phone: unknown };
  summary: string;
  // Empty for the pick (it needs no explaining); a code-written one-line sentence for
  // the runner-up, built from its own `summary` — never a model-authored string.
  why: string;
}

interface BoundFormShape {
  type?: unknown;
  clir?: { desktop?: unknown; phone?: unknown };
  summary?: unknown;
  alternatives?: unknown;
}

interface AlternativeShape {
  clir?: { desktop?: unknown; phone?: unknown };
  summary?: unknown;
}

function asClirFaces(v: { desktop?: unknown; phone?: unknown } | undefined): { desktop: unknown; phone: unknown } | null {
  console.assert(v === undefined || typeof v === "object", "asClirFaces: shape is object|undefined");
  if (!v || typeof v !== "object") return null;
  if (v.desktop === undefined && v.phone === undefined) return null;
  return { desktop: v.desktop, phone: v.phone };
}

/** `payload` is `item.payload` (unknown — the board's bound JSON). [] when it is not a
 *  bound form; otherwise the pick (always, first) and, when the payload carries one
 *  lint-clean `alternatives` entry, the runner-up second. */
export function verifyOptions(payload: unknown): VerifyOption[] {
  console.assert(payload !== undefined, "verifyOptions: payload defined");
  console.assert(!Array.isArray(payload), "verifyOptions: payload is the board's one bound node, not a list");
  if (!payload || typeof payload !== "object") return [];
  const p = payload as BoundFormShape;
  const pickClir = p.type === "form" ? asClirFaces(p.clir) : null;
  if (!pickClir) return [];
  const pick: VerifyOption = {
    id: "pick",
    clir: pickClir,
    summary: typeof p.summary === "string" ? p.summary : "",
    why: "",
  };
  const alts = Array.isArray(p.alternatives) ? p.alternatives.slice(0, 1) : [];
  const second = alts[0] as AlternativeShape | undefined;
  const secondClir = second ? asClirFaces(second.clir) : null;
  if (!second || !secondClir) return [pick];
  const summary = typeof second.summary === "string" ? second.summary : "";
  return [
    pick,
    { id: "second", clir: secondClir, summary, why: summary ? `Shown differently: ${summary}` : "A different design of the same data." },
  ];
}

/** The CSS max-width a VerifyPanel option box gets, so a face previews at the width the
 *  card will actually have on the board instead of stretching across the panel: a
 *  1-column desktop face takes half the panel (the panel sits inside a 2-column
 *  verifying card, so half of it is one board column), a phone face its bucket's
 *  ceiling (spans.py PHONE_W: full width 366 + 32 padding, half width 160 outer); a
 *  2+-column desktop face, and a CLIR without a parseable span key, fill the panel. */
export function optionMaxWidth(clir: unknown): string | null {
  console.assert(clir === null || clir === undefined || typeof clir === "object", "optionMaxWidth: clir is an object or null");
  const key = clir && typeof clir === "object" ? (clir as { span?: unknown }).span : undefined;
  const m = typeof key === "string" ? /^([dp])([1-4])x([1-3])$/.exec(key) : null;
  console.assert(m === null || (m[1] === "d" || m[1] === "p"), "optionMaxWidth: device is d or p");
  if (!m) return null;
  if (m[1] === "p") return m[2] === "1" ? "160px" : "398px";
  return m[2] === "1" ? "calc(50% - var(--s-2))" : null;
}
