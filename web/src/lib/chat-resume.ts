// Rune-free, unit-testable resume logic for the open chat (the persisted session lives in
// chat.svelte.ts; kept separate so this is importable under vitest).
//
// Regression #11: resuming the open conversation must fetch it DIRECTLY — never gate it on the
// conversation-LIST load. A transient list failure used to leave an empty page that looked
// like a brand-new chat. Returns the conversation's messages, or [] when there's nothing to
// resume. On a confirmed 404 (deleted) it clears currentId so it can't error forever; a
// transient error is rethrown WITHOUT dropping currentId, so the next visit retries.

export async function resumeChat<M>(
  session: { currentId: string | null },
  getConversation: (id: string) => Promise<{ messages: M[] }>,
  isNotFound: (err: unknown) => boolean,
): Promise<M[]> {
  const id = session.currentId;
  if (!id) return [];
  try {
    return (await getConversation(id)).messages;
  } catch (err) {
    if (isNotFound(err)) {
      session.currentId = null;
      return [];
    }
    throw err;
  }
}

// A resume that lands while an approved tool of the turn is still running answers
// awaiting_approval with in_flight and no pending cards (the server never holds the
// request). The page keeps the turn and retries on this cadence, bounded (~2 min).
export const IN_FLIGHT_RETRY_MS = 2000;
export const IN_FLIGHT_MAX_RETRIES = 60;

// Delay before the next resume attempt, or null when the response is not an in-flight
// wait or the retry budget is spent. `attempt` counts the retries already made.
export function inFlightRetryDelay(
  res: { status: string; in_flight?: boolean; pending?: unknown[] },
  attempt: number,
): number | null {
  console.assert(Number.isInteger(attempt) && attempt >= 0, "attempt must be a non-negative integer");
  const waiting = res.status === "awaiting_approval" && res.in_flight === true && !res.pending?.length;
  if (!waiting || attempt >= IN_FLIGHT_MAX_RETRIES) return null;
  return IN_FLIGHT_RETRY_MS;
}
