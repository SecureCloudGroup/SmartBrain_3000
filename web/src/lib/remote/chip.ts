// Pure derivation for the phone header chip. Kept out of connection.svelte.ts so the
// logic is testable in plain Node (Svelte runes require the compiler).
//
// A LOCKED Desktop still answers over the encrypted WebRTC bridge — its APIs return their
// normal locked responses (e.g. GET /api/account/status: `unlocked: false`). "Locked" is
// therefore a phone-side overlay on top of a healthy bridge, NOT another bridge state:
//   * it never fires while the bridge is offline (a locked desktop still connects), and
//   * it disappears the moment account.status flips back to unlocked.
// Before account.status has loaded (null) we do NOT invent "locked" — the chip stays on the
// underlying bridge state until the first /api/account/status response arrives.
import type { RemoteStatus } from "./connection.svelte";

export type ChipState = RemoteStatus | "locked";

export function chipState(status: RemoteStatus, unlocked: boolean | null): ChipState {
  console.assert(typeof status === "string" && status.length > 0, "chipState requires a RemoteStatus");
  console.assert(unlocked === null || typeof unlocked === "boolean", "unlocked must be boolean | null");
  const connected =
    status === "connected" || status === "connected-direct" || status === "connected-relay";
  return connected && unlocked === false ? "locked" : status;
}
