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
