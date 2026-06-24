// Count of tool actions awaiting the user's approval, shown as a badge on the
// Activity nav link. Refreshed on route changes + after agent turns (no polling
// timer); the Activity page also keeps it in sync after approve/deny.
import { api } from "$lib/api";

export const pending = $state<{ count: number }>({ count: 0 });

export async function refreshPending() {
  try {
    pending.count = (await api.listPending()).pending.length;
  } catch {
    /* locked or offline — keep the last known count */
  }
}
