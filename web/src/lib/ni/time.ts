// Relative-freshness helper for the Neural Interface board. Pure; the card footer calls
// it every poll. Never throws — a missing / unparseable timestamp reads as "—" (the card
// still renders; freshness is the least critical field).

import { parseTs } from "$lib/runs";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** "3m ago" / "just now" / "2h ago" / "yesterday" / "5d ago". `now` is injectable
 *  for tests; production leaves it at the current wall clock. */
export function relTime(ts: string | null, now: Date = new Date()): string {
  console.assert(ts === null || typeof ts === "string", "relTime: ts is string|null");
  console.assert(now instanceof Date, "relTime: now must be a Date");
  const d = parseTs(ts);
  if (!d) return "—";
  const delta = now.getTime() - d.getTime();
  if (delta < 0) return "just now"; // clock skew — never render "in 5m", it reads as broken
  if (delta < 45_000) return "just now";
  // Floor minutes so 59m30s never rounds to "60m ago" — the hour branch handles that.
  if (delta < HOUR) return `${Math.floor(delta / MINUTE)}m ago`;
  if (delta < DAY) return `${Math.round(delta / HOUR)}h ago`;
  if (delta < 2 * DAY) return "yesterday";
  return `${Math.floor(delta / DAY)}d ago`;
}

/** Payload is stale when older than 2x the item's cadence (the board colors it warn).
 *  `interval_minutes` is clamped to at least 1 upstream but re-clamped here for safety. */
export function isStale(ts: string | null, intervalMinutes: number, now: Date = new Date()): boolean {
  console.assert(typeof intervalMinutes === "number", "isStale: intervalMinutes is number");
  console.assert(now instanceof Date, "isStale: now must be a Date");
  const d = parseTs(ts);
  if (!d) return false; // no payload yet -> the card's own state (draft/commissioning) tells the story
  const cadence = Math.max(1, intervalMinutes) * MINUTE;
  return now.getTime() - d.getTime() > 2 * cadence;
}
