// Count of scheduled-run outputs the user hasn't opened yet, shown as a badge on the
// Chat nav link. Refreshed on route changes (no polling timer), same as pending.svelte.ts;
// the Scheduled updates feed clears it after marking runs seen.
import { api } from "$lib/api";

export const scheduleUpdates = $state<{ count: number }>({ count: 0 });

export async function refreshScheduleUpdates() {
  try {
    scheduleUpdates.count = (await api.unseenScheduleUpdates()).count;
  } catch {
    /* locked or offline — keep the last known count */
  }
}
