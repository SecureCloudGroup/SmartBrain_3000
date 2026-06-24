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
//   offline           can't reach the Desktop (it may be off or locked)

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
