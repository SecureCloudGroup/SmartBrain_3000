// DataChannel wire protocol for remote access — mirrors app/smartbrain_3000/webrtc_peer.py.
//
// JSON text messages over a DTLS-encrypted WebRTC DataChannel. Request/response bodies
// are base64 so binary payloads (uploads, encrypted backups) are safe. Handshake order:
//   1. hello   {type:"hello", nonce}            -> hello_ok {type, pubkey, signature}
//   2. auth    {type:"auth", device_id, credential} -> {type:"auth_ok"} | {type:"auth_error"}
//   3. request {id, method, path, headers, body_b64} -> response {id, status, headers, body_b64}

export interface ResponseFrame {
  id: string;
  status: number;
  headers: Record<string, string>;
  body_b64: string;
}

export interface ParsedResponse {
  id: string;
  status: number;
  headers: Record<string, string>;
  body: Uint8Array;
}

export function bytesToB64(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

export function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function concatBytes(a: Uint8Array, b: Uint8Array): Uint8Array {
  const out = new Uint8Array(a.length + b.length);
  out.set(a);
  out.set(b, a.length);
  return out;
}

export function encodeHello(nonceB64: string): string {
  return JSON.stringify({ type: "hello", nonce: nonceB64 });
}

export function encodeAuth(deviceId: string, credential: string): string {
  return JSON.stringify({ type: "auth", device_id: deviceId, credential });
}

export function encodeRequest(
  id: string,
  method: string,
  path: string,
  headers: Record<string, string>,
  body: Uint8Array,
): string {
  return JSON.stringify({ id, method, path, headers, body_b64: bytesToB64(body) });
}

// A control message is one with a "type" (hello_ok / auth_ok / auth_error); a response
// frame has a numeric "status". Callers branch on which shape came back.
export function parseMessage(text: string): { type?: string; [k: string]: unknown } {
  const m = JSON.parse(text);
  if (typeof m !== "object" || m === null) throw new Error("message must be a JSON object");
  return m as { type?: string };
}

export function asResponse(m: Record<string, unknown>): ParsedResponse {
  return {
    id: String(m.id ?? ""),
    status: Number(m.status),
    headers: (m.headers as Record<string, string>) ?? {},
    body: b64ToBytes(String(m.body_b64 ?? "")),
  };
}
