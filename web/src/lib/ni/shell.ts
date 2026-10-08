// Pure helpers behind CardShell.svelte (ni-format §34, Phase 1a-4 "the card shell").
// The component stays thin — every item -> appearance decision lives here, unit-tested
// without mounting Svelte. Nothing here touches the DOM or the untrusted payload.

import { parseTs } from "$lib/runs";
import { isStale } from "$lib/ni/time";
import type { NiBoardItem, NiDisplay } from "$lib/api";

/** The 8 buckets CardShell renders by. Distinct from the backend's `NiState` string:
 *  this groups a few NiState values under one visual treatment (e.g. "degraded" reads
 *  the same as "failing" — both show the dimmed last-good body + reason + Fix). */
export type CardState =
  | "designing"
  | "verifying"
  | "fresh"
  | "stale"
  | "failing"
  | "broken"
  | "empty"
  | "paused";

/** One item -> one CardState. Priority (first match wins), each tied to what the board
 *  already renders for that case: an active/queued build (or a never-activated draft)
 *  reads as "designing"; the user's own Pause toggle (or the rarely-set NiState
 *  "paused" — no production path sets it, per ni.py) always wins next, so a paused
 *  card never also looks "failing"; "broken" is its own terminal copy; a commissioning
 *  card with a reviewable payload (the held awaiting_yes screen, or the plain "is this
 *  right" C2 screen) is "verifying", else it has nothing to show yet ("empty", the
 *  existing Waiting/First-run-failed copy); once settled, "failing"/"degraded" share the
 *  dimmed-body treatment, and a live card is "fresh" or "stale" by the existing
 *  2x-cadence rule. */
export function cardState(item: NiBoardItem): CardState {
  console.assert(typeof item.state === "string", "cardState: state is string");
  console.assert(typeof item.enabled === "boolean", "cardState: enabled is boolean");
  if (item.flow || item.state === "draft") return "designing";
  if (!item.enabled || item.state === "paused") return "paused";
  if (item.state === "broken") return "broken";
  if (item.state === "commissioning") {
    const reviewable = item.awaiting_yes != null || item.payload_slot !== "preview";
    return item.payload !== null && reviewable ? "verifying" : "empty";
  }
  if (item.payload === null) return "empty";
  if (item.state === "failing" || item.state === "degraded") return "failing";
  return isStale(item.payload_at, item.interval_minutes) ? "stale" : "fresh";
}

/** The mandatory non-form footer line: "as of <local time> [· <host>] · every Nm". A
 *  bound form payload's CLIR already prints its own as-of/host/cadence line (ni.py
 *  `_bind_form`'s shell), so CardShell never calls this for one. `host` only exists on
 *  the board row for a held (`awaiting_yes`) item — every other NiBoardItem carries no
 *  source URL, so the segment is dropped rather than invented. */
export function footerText(item: NiBoardItem): string {
  console.assert(typeof item.interval_minutes === "number", "footerText: interval is number");
  console.assert(
    item.payload_at === null || typeof item.payload_at === "string",
    "footerText: payload_at shape",
  );
  const when = parseTs(item.payload_at);
  const asOf = when
    ? `as of ${new Intl.DateTimeFormat(undefined, { timeStyle: "short" }).format(when)}`
    : "as of —";
  const host = item.awaiting_yes?.host ?? "";
  const cadence = `every ${Math.max(1, item.interval_minutes)}m`;
  return [asOf, host, cadence].filter((part) => part.length > 0).join(" · ");
}

/** One card -> its grid footprint `{cols, rows}`. The painted CLIR's own span key
 *  (`d1x2`, `p2x3`, … — spans.py stamps one on every CLIR) wins when present: the engine
 *  picks a phone span per candidate (`phone_default`: a 1-column desktop design may seal
 *  a full-width 2- or 3-row phone face), so the phone face can need a different footprint
 *  from the desktop face, and `display.size` only describes the desktop one. Without a
 *  parseable key (a legacy scene, a payload from an older server) `display.size` maps
 *  small 1x1, wide 2x1, large 2x2, tall 1x2; absent/unknown reads as 1x1 (the
 *  version-skew rule). Columns clamp at 2 (the grid has 2 phone / 3-4 desktop columns —
 *  a wider design paints scaled down inside 2, never breaks the grid); rows clamp at 3
 *  (spans.py's ceiling). */
export function cardSpan(
  size: NiDisplay["size"] | undefined,
  clir: unknown,
): { cols: 1 | 2; rows: 1 | 2 | 3 } {
  console.assert(size === undefined || typeof size === "string", "cardSpan: size is a string or undefined");
  console.assert(clir === null || clir === undefined || typeof clir === "object", "cardSpan: clir is an object or null");
  const key = clir && typeof clir === "object" ? (clir as { span?: unknown }).span : undefined;
  const m = typeof key === "string" ? /^[dp]([1-4])x([1-3])$/.exec(key) : null;
  if (m) {
    return { cols: Number(m[1]) >= 2 ? 2 : 1, rows: Number(m[2]) as 1 | 2 | 3 };
  }
  switch (size) {
    case "wide":
      return { cols: 2, rows: 1 };
    case "large":
      return { cols: 2, rows: 2 };
    case "tall":
      return { cols: 1, rows: 2 };
    default:
      return { cols: 1, rows: 1 };
  }
}

/** True when the card BODY must render nothing because VerifyPanel — mounted
 *  separately by the page, not here — already IS the card view for this screen; a
 *  second IrPaint of the same CLIR directly above it would just repeat the pick.
 *  Mirrors, field for field, the exact condition +page.svelte uses to decide whether
 *  it mounts VerifyPanel at all, so the two can never disagree about this one screen:
 *  commissioning, a non-preview payload present, not yet confirmed (`c2_ok`), not the
 *  separate held-source "is this what you asked for" screen (unchanged, ruled
 *  2026-10-07), and the payload is in fact a bound form (VerifyPanel's precondition —
 *  a non-form item never reaches this branch, so the body keeps painting NiScene). */
export function bodyShowsPanel(item: NiBoardItem, hasForm: boolean): boolean {
  console.assert(typeof item.state === "string", "bodyShowsPanel: state is string");
  console.assert(typeof hasForm === "boolean", "bodyShowsPanel: hasForm is boolean");
  if (!hasForm || item.flow) return false;
  if (item.awaiting_yes && item.payload) return false; // held-source screen: unchanged
  if (item.state !== "commissioning" || item.c2_ok) return false;
  return item.payload !== null && item.payload_slot !== "preview";
}

/** True when CardShell may shrink to exactly the CLIR's own box (spans.py: rows x
 *  176 + (rows-1) x gap - 32, 16px padding, no head/foot rows) instead of the legacy
 *  24px chrome with a normal head/foot. Narrowly scoped to the three states whose body
 *  is ALWAYS a bare, empty-`below` CLIR plane — "verifying" shows VerifyPanel (taller
 *  than any design) and "designing" shows flow/activate copy, and are excluded for
 *  that reason; "failing"/"broken"/commissioning-with-failures also keep the legacy
 *  chrome here because they can carry the reason line and the Fix affordance (`below`)
 *  alongside the body, which the tight, overlay-chip chrome has no safe room for. Both
 *  CardShell (the CSS gate) and the page (the actions-overlay shape) call this so they
 *  never disagree about which chrome a given card gets. */
export function formBoxChrome(state: CardState, hasForm: boolean): boolean {
  console.assert(typeof hasForm === "boolean", "formBoxChrome: hasForm is boolean");
  console.assert(typeof state === "string", "formBoxChrome: state is string");
  return hasForm && (state === "fresh" || state === "stale" || state === "paused");
}
