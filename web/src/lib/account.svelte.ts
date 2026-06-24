// Shared, reactive account state (Svelte 5 runes). Pages read `account.status`
// to guard their routes; `load()` refreshes it from the backend. Concurrent
// callers (the root layout + a page guard on cold start) share one in-flight
// request rather than firing duplicates.
import { ApiError, type AccountStatus, api } from "./api";

class Account {
  status = $state<AccountStatus | null>(null);
  loading = $state(false);
  error = $state("");
  #inflight: Promise<void> | null = null;

  load(): Promise<void> {
    if (this.#inflight) return this.#inflight;
    this.#inflight = this.#run();
    return this.#inflight;
  }

  async #run(): Promise<void> {
    this.loading = true;
    this.error = "";
    try {
      this.status = await api.accountStatus();
    } catch (err) {
      // Leave status as-is and surface the failure so the UI can offer a retry
      // instead of hanging on a blank "Loading…" forever.
      this.error = err instanceof ApiError ? err.message : "Cannot reach the backend.";
    } finally {
      this.loading = false;
      this.#inflight = null;
    }
  }
}

export const account = new Account();
