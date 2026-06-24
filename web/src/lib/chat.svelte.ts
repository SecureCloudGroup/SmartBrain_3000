// Keeps the open conversation across navigation. Leaving /chat and returning
// resumes the same chat instead of silently starting a new one — a new chat is
// only started when the user clicks "+ New chat". currentId is null for a fresh,
// not-yet-saved chat. The rune-free resume logic lives in chat-resume.ts (testable).
export const chatSession = $state<{ currentId: string | null }>({ currentId: null });
