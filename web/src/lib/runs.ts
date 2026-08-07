// Shared helpers for rendering server timestamps + schedule-run status.
// Lifted from the Schedules page when run output moved to the Info page, so both
// (and the chat trash view) render the same way.

/** Parse a server timestamp into a Date, or null if unparseable.
 *
 * The backend stores and sends UTC everywhere (the DB session is pinned to UTC —
 * see db.open_db), in one of two shapes: naive ("2026-06-21 14:30:00[.ffffff]"),
 * which is treated as UTC, or ISO with an explicit offset/Z, which is respected
 * as written.
 */
export function parseTs(s: string | null): Date | null {
  if (!s) return null;
  const iso = s.replace(" ", "T");
  const d = /(?:[zZ]|[+-]\d\d:?\d\d)$/.test(iso)
    ? new Date(iso)
    : new Date(iso.slice(0, 19) + "Z");
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Server timestamps rendered in the user's locale (date + time). */
export function localTs(s: string | null): string {
  const d = parseTs(s);
  return d ? d.toLocaleString() : (s ?? "");
}

/** Human label for a schedule run's status. */
export function runStatusLabel(status: string): string {
  if (status === "awaiting_approval") return "Needs approval";
  if (status === "error") return "Failed";
  if (status === "complete") return "Done";
  return status;
}

/** Whole days left before a trashed item purges: retention minus elapsed, floored at 0. */
export function daysLeft(deletedAt: string, retentionDays: number, now: Date = new Date()): number {
  const d = parseTs(deletedAt);
  if (!d) return retentionDays;
  const elapsedDays = Math.floor((now.getTime() - d.getTime()) / 86_400_000);
  return Math.max(0, retentionDays - elapsedDays);
}
