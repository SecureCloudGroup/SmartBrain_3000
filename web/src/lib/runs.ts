// Shared helpers for rendering server timestamps + schedule-run status.
// Lifted from the Schedules page when run output moved to the Info page, so both
// (and the chat trash view) render the same way.

/** Server timestamps are UTC strings (e.g. "2026-06-21 14:30:00") — render in the user's locale. */
export function localTs(s: string | null): string {
  if (!s) return "";
  const d = new Date(s.slice(0, 19).replace(" ", "T") + "Z");
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString();
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
  const d = new Date(deletedAt.slice(0, 19).replace(" ", "T") + "Z");
  if (Number.isNaN(d.getTime())) return retentionDays;
  const elapsedDays = Math.floor((now.getTime() - d.getTime()) / 86_400_000);
  return Math.max(0, retentionDays - elapsedDays);
}
