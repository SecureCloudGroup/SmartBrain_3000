// Pairing payload: what the phone stores at pairing so it can later reach the Desktop
// from anywhere and verify it. The Desktop renders this as a QR (a deep link with the
// payload in the URL FRAGMENT, which never leaves the device / never hits a server);
// the phone reads it from location.hash, validates, and persists it.

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

// URL-safe base64 of the UTF-8 JSON, for the QR deep-link fragment.
export function encodePairingFragment(payload: PairingPayload): string {
  const json = JSON.stringify(payload);
  const b64 = btoa(String.fromCharCode(...new TextEncoder().encode(json)));
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function decodePairingFragment(hash: string): PairingPayload {
  const frag = hash.replace(/^#/, "");
  if (!frag) throw new Error("no pairing data in URL");
  const b64 = frag.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return parsePairingPayload(new TextDecoder().decode(bytes));
}
