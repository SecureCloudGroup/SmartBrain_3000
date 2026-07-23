// Pure helpers over the chat page's message log — extracted (like chat-resume) so the
// Regenerate semantics are unit-testable without rendering the page. A log entry here is
// the page's Entry shape minus the display-only extras it doesn't need.

export interface LogEntry {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  err?: boolean; // an error bubble — shown locally, never persisted server-side
  schedule?: boolean; // an injected scheduled-run notice — display-only, not part of the thread
}

// The id of the FINAL assistant answer in the thread, or null when the thread doesn't
// end on one. Regenerate is only offered here: regenerating an older answer would fork
// a thread whose later turns already built on it. Schedule notices are skipped (they
// don't end the thread); an errored bubble was never a real answer.
export function finalAssistantId(entries: LogEntry[]): string | null {
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    const e = entries[i];
    if (e.schedule) continue;
    return e.role === "assistant" && !e.err ? e.id : null;
  }
  return null;
}

// The transcript for a REGENERATED turn: history up to (and including) the last user
// message — i.e. the previous answer is left out so the model answers that message
// afresh. Errored bubbles and schedule notices are dropped for the same reason
// buildTranscript drops them: neither was ever persisted as part of the thread.
// Returns null when there's no user message to regenerate from.
export function transcriptUpToLastUser(
  entries: LogEntry[],
): { role: LogEntry["role"]; content: string }[] | null {
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    if (entries[i].role === "user" && !entries[i].err && !entries[i].schedule) {
      return entries
        .slice(0, i + 1)
        .filter((e) => !e.err && !e.schedule)
        .map(({ role, content }) => ({ role, content }));
    }
  }
  return null;
}

// The log after a REFRESH (manual button or returning to the app): server messages are
// the truth for the persisted thread, but the page also holds entries that exist ONLY
// locally — injected scheduled-run notices (already marked seen server-side; a naive
// replace would lose them forever, nowhere else re-shows them). Those are re-appended.
// Errored bubbles are dropped, exactly as a full reload would drop them: they were
// never persisted. Older paged-in messages collapse back to the newest page — the same
// semantics as opening the thread fresh; "Load older" re-pages.
export function mergeRefreshedLog(serverEntries: LogEntry[], currentLog: LogEntry[]): LogEntry[] {
  const schedules = currentLog.filter((e) => e.schedule);
  return [...serverEntries, ...schedules];
}
