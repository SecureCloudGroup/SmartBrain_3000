// Pairing payload: what the phone stores at pairing so it can later reach the Desktop
// from anywhere and verify it. The phone receives it over an encrypted WebRTC channel
// after the operator enters the 6-char code (see paircode.ts), validates, and persists it.

export interface PairingPayload {
  v: number;
  deviceId: string; // public device id (signaling routing)
  credential: string; // per-device bearer secret (sent only inside DTLS)
  desktopPubkey: string; // Ed25519 pubkey to PIN (verifies the Desktop over the channel)
  signalingUrl: string; // wss:// broker URL
  desktopId: string; // which Desktop to reach via the broker
  iceServers: RTCIceServer[]; // STUN/TURN + shared relay credentials (static, not per-device)
}

const _REQUIRED: Array<keyof PairingPayload> = ["deviceId", "credential", "desktopPubkey", "signalingUrl"];

export function parsePairingPayload(raw: string): PairingPayload {
  const o = JSON.parse(raw) as Record<string, unknown>;
  if (typeof o !== "object" || o === null) throw new Error("invalid pairing payload");
  for (const k of _REQUIRED) {
    if (typeof o[k] !== "string" || !(o[k] as string)) throw new Error(`pairing payload missing ${k}`);
  }
  return {
    v: Number(o.v) || 1,
    deviceId: o.deviceId as string,
    credential: o.credential as string,
    desktopPubkey: o.desktopPubkey as string,
    signalingUrl: o.signalingUrl as string,
    desktopId: String(o.desktopId ?? ""),
    iceServers: Array.isArray(o.iceServers) ? (o.iceServers as RTCIceServer[]) : [],
  };
}
