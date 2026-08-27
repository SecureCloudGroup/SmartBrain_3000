// Pairing payload: what the phone stores at pairing so it can later reach the Desktop
// from anywhere and verify it. The phone receives it over an encrypted WebRTC channel
// after the operator enters the 8-char code (see paircode.ts), validates, and persists it.

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
const _ICE_SCHEMES = ["stun:", "turn:", "turns:"];

// The payload names the broker we will dial and the ICE servers we will hand the browser
// forever after. A payload that points at ws:// (plaintext signaling) or at a non-ICE URL
// (e.g. an http: "TURN" that would make the browser talk to an arbitrary host) is refused.
function checkIceServers(raw: unknown): RTCIceServer[] {
  if (raw === undefined || raw === null) return [];
  if (!Array.isArray(raw)) throw new Error("pairing payload iceServers must be a list");
  for (const s of raw) {
    if (typeof s !== "object" || s === null) throw new Error("pairing payload iceServers entry must be an object");
    const urls = (s as { urls?: unknown }).urls;
    const list = typeof urls === "string" ? [urls] : urls;
    if (!Array.isArray(list) || !list.length) throw new Error("pairing payload iceServers entry needs urls");
    for (const u of list) {
      if (typeof u !== "string" || !_ICE_SCHEMES.some((p) => u.startsWith(p))) {
        throw new Error("pairing payload iceServers urls must be stun:/turn:/turns:");
      }
    }
  }
  return raw as RTCIceServer[];
}

export function parsePairingPayload(raw: string): PairingPayload {
  const o = JSON.parse(raw) as Record<string, unknown>;
  if (typeof o !== "object" || o === null) throw new Error("invalid pairing payload");
  for (const k of _REQUIRED) {
    if (typeof o[k] !== "string" || !(o[k] as string)) throw new Error(`pairing payload missing ${k}`);
  }
  if (!(o.signalingUrl as string).startsWith("wss://")) throw new Error("pairing payload signalingUrl must be wss://");
  return {
    v: Number(o.v) || 1,
    deviceId: o.deviceId as string,
    credential: o.credential as string,
    desktopPubkey: o.desktopPubkey as string,
    signalingUrl: o.signalingUrl as string,
    desktopId: String(o.desktopId ?? ""),
    iceServers: checkIceServers(o.iceServers),
  };
}
