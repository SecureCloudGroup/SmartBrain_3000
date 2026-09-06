// Keeps the open conversation across navigation. Leaving /chat and returning
// resumes the same chat instead of silently starting a new one — a new chat is
// only started when the user clicks "+ New chat". currentId is null for a fresh,
// not-yet-saved chat. The rune-free resume logic lives in chat-resume.ts (testable).
//
// pickedModel: the user's manual model pick for THIS app session (in memory only,
// deliberately never persisted). Visiting another tab and returning must not
// silently flip the picker back to the routed default (v0.9.36 field report:
// "changed to haiku, ran chats, looked and it was back on sonnet") — while a
// fresh app launch still opens on the routed default, which stays the shared,
// persisted source of truth (the original stale-persisted-pick fix holds).
export const chatSession = $state<{ currentId: string | null; pickedModel: string | null }>(
  { currentId: null, pickedModel: null },
);
