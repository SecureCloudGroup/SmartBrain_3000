// DataChannel wire protocol for remote access — mirrors app/smartbrain_3000/webrtc_peer.py.
//
// JSON text messages over a DTLS-encrypted WebRTC DataChannel. Request/response bodies
// are base64 so binary payloads (uploads, encrypted backups) are safe. Handshake order:
//   1. hello   {type:"hello", nonce}            -> hello_ok {type, pubkey, signature}
//   2. auth    {type:"auth", device_id, credential} -> {type:"auth_ok"} | {type:"auth_error"}
//   3. request {id, method, path, headers, body_b64} -> response {id, status, headers, body_b64}
//      Requests advertise {chunks:true}: a response too big for one channel message then
//      arrives as ORDERED part-frames {id, seq, more, body_b64} (the first also carries
//      status + headers) instead of a 413. Old Desktops ignore the flag and still 413.
//   4. keepalive (authed only): ping {type:"ping", t} -> pong {type:"pong", t}. The phone
//      sends these on a timer; a missing pong past the deadline means the path silently
//      died (idle NAT/consent expiry) even though connectionState still says "connected".

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

export function encodePing(t: number): string {
  return JSON.stringify({ type: "ping", t });
}

// Has the keepalive gone unanswered past the deadline? lastPong === 0 means "no pong
// expected yet" (timer not started / just connected) and is never dead.
export function pingDead(lastPongMs: number, nowMs: number, deadMs: number): boolean {
  return lastPongMs > 0 && nowMs - lastPongMs > deadMs;
}

export function encodeRequest(
  id: string,
  method: string,
  path: string,
  headers: Record<string, string>,
  body: Uint8Array,
): string {
  // chunks:true = this phone can reassemble part-framed responses (big audit feeds,
  // large documents). An old Desktop ignores the key and 413s oversized responses.
  return JSON.stringify({ id, method, path, headers, body_b64: bytesToB64(body), chunks: true });
}

// --- chunked responses -------------------------------------------------------------------
// A part-frame is {id, seq, more, body_b64} (+ status/headers on seq 0). The DataChannel
// is reliable + ordered, so seq is an integrity cross-check, not a reordering mechanism.

export interface ChunkState {
  id: string;
  status: number;
  headers: Record<string, string>;
  parts: string[];
  nextSeq: number;
  totalChars: number;
}

// Bound what one response may accumulate on the phone (mirrors the Desktop's 8 MB
// response ceiling with base64's 4/3 overhead + slack).
const _MAX_CHUNK_TOTAL_CHARS = 12 * 1024 * 1024;

export function isChunkFrame(m: Record<string, unknown>): boolean {
  return typeof m.seq === "number" && typeof m.more === "boolean";
}

/** Fold one part-frame into the accumulating state.
 *  Returns the updated state, the finished response (`done`), or an `error` string when
 *  the stream is malformed (wrong seq, missing head, over the bound) — the caller drops
 *  the transfer and fails the request. Pure, so it is unit-testable without a channel. */
export function appendChunk(
  state: ChunkState | null,
  m: Record<string, unknown>,
): { state?: ChunkState; done?: ParsedResponse; error?: string } {
  const seq = Number(m.seq);
  if (state === null) {
    if (seq !== 0 || typeof m.status !== "number") return { error: "chunk stream must start at seq 0 with a status" };
    state = { id: String(m.id ?? ""), status: Number(m.status), headers: (m.headers as Record<string, string>) ?? {}, parts: [], nextSeq: 0, totalChars: 0 };
  }
  if (seq !== state.nextSeq) return { error: `chunk out of order (got ${seq}, expected ${state.nextSeq})` };
  const piece = String(m.body_b64 ?? "");
  state.totalChars += piece.length;
  if (state.totalChars > _MAX_CHUNK_TOTAL_CHARS) return { error: "chunked response exceeds the size bound" };
  state.parts.push(piece);
  state.nextSeq += 1;
  if (m.more === true) return { state };
  return { done: { id: state.id, status: state.status, headers: state.headers, body: b64ToBytes(state.parts.join("")) } };
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
