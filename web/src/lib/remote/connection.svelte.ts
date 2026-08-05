// Reactive remote-connection status the UI binds to (RemoteStatus chip + layout).
//
//   idle              not in remote mode (on the Desktop LAN, /api is used directly)
//   connecting        establishing the WebRTC connection
//   verifying         got the Desktop's identity proof; checking it against the pinned key
//   connected-direct  P2P, no relay (confirmed)
//   connected-relay   going through the content-blind TURN relay (direct wasn't possible)
//   connected         connected, but direct-vs-relay couldn't be determined (don't over-claim)
//   reconnecting      link dropped; retrying
//   untrusted         Desktop identity FAILED to verify — refused (possible MITM); no data sent
//   offline           can't reach the Desktop at all (off, asleep, or no network path)
//
// A LOCKED Desktop is deliberately NOT in this list: the bridge has no coupling to the vault
// lock and a locked Desktop still connects and answers (its APIs return their normal locked
// responses). "Locked" is a phone-side overlay derived from `connected + account.unlocked=false`
// — see chip.ts, which the chip component uses to present it distinctly.

export type RemoteStatus =
  | "idle"
  | "connecting"
  | "verifying"
  | "connected"
  | "connected-direct"
  | "connected-relay"
  | "reconnecting"
  | "untrusted"
  | "offline";

// `needsPairing` = off the LAN with no stored pairing (a fresh phone / installed app): show a
// friendly "pair this device" welcome instead of the scary "can't reach" outage card.
export const remote = $state<{ status: RemoteStatus; detail: string; needsPairing: boolean }>({
  status: "idle",
  detail: "",
  needsPairing: false,
});

export function setRemoteStatus(status: RemoteStatus, detail = ""): void {
  remote.status = status;
  remote.detail = detail;
}

export function isConnected(): boolean {
  return remote.status === "connected" || remote.status === "connected-direct" || remote.status === "connected-relay";
}
