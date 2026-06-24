// Pairing-by-code on the APP side — the installed (home-screen) PWA can't inherit the
// Safari pairing (iOS isolates its storage), so it fetches the pairing itself: the operator
// reads a 6-char code off the Desktop, types it here, and we connect to the Desktop over
// WebRTC (relayed by the broker), prove mutual knowledge of the code bound to the DTLS
// channel, and receive the PairingPayload inside the encrypted channel.
//
// The derivation MUST stay byte-identical to app/smartbrain_3000/pairing_code.py.
// NOTE: the broker hands the pairing client the node's ICE config (STUN + TURN) when it
// joins a pairing room, so pairing works on Wi-Fi AND cellular. If the broker doesn't send
// it (older broker), we fall back to the node's STUN only — fine on Wi-Fi / non-symmetric
// NAT, but a symmetric-NAT phone (some cellular) then needs Wi-Fi to pair.

import { channelBinding } from "./crypto";
import { parsePairingPayload, type PairingPayload } from "./pairing";
import { b64ToBytes, bytesToB64 } from "./protocol";

const _ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"; // must match pairing_code.py
const _SALT = "sb-pair-v1";
const _ITERS = 300_000;
const _CODE_LEN = 6;

export function normalizeCode(code: string): string {
  return [...code.toUpperCase()].filter((c) => _ALPHABET.includes(c)).join("");
}

export async function deriveCode(code: string): Promise<{ roomId: string; codeKey: CryptoKey }> {
  const norm = normalizeCode(code);
  if (norm.length !== _CODE_LEN) throw new Error("the code is 6 characters");
  const enc = new TextEncoder();
  const base = await crypto.subtle.importKey("raw", enc.encode(norm), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: enc.encode(_SALT), iterations: _ITERS, hash: "SHA-256" },
    base,
    (16 + 32) * 8,
  );
  const dk = new Uint8Array(bits);
  const roomId = "sbpair-" + [...dk.slice(0, 16)].map((b) => b.toString(16).padStart(2, "0")).join("");
  const codeKey = await crypto.subtle.importKey("raw", dk.slice(16, 48), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return { roomId, codeKey };
}

export async function mac(codeKey: CryptoKey, label: string, nonce: Uint8Array, binding: Uint8Array): Promise<Uint8Array> {
  const lb = new TextEncoder().encode(label);
  const msg = new Uint8Array(lb.length + nonce.length + binding.length);
  msg.set(lb, 0);
  msg.set(nonce, lb.length);
  msg.set(binding, lb.length + nonce.length);
  return new Uint8Array(await crypto.subtle.sign("HMAC", codeKey, msg));
}

function macEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

function iceComplete(pc: RTCPeerConnection, timeoutMs = 3000): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise<void>((resolve) => {
    const done = () => {
      if (pc.iceGatheringState !== "complete") return;
      pc.removeEventListener("icegatheringstatechange", done);
      clearTimeout(t);
      resolve();
    };
    const t = setTimeout(() => {
      pc.removeEventListener("icegatheringstatechange", done);
      resolve();
    }, timeoutMs);
    pc.addEventListener("icegatheringstatechange", done);
  });
}

// Run one pairing-by-code exchange; resolve with the PairingPayload or reject with a reason.
export async function pairByCode(code: string, timeoutMs = 30_000): Promise<PairingPayload> {
  const { roomId, codeKey } = await deriveCode(code);
  const signalingUrl = `wss://${window.location.host}/signal`;
  return new Promise<PairingPayload>((resolve, reject) => {
    const ws = new WebSocket(signalingUrl);
    let pc: RTCPeerConnection | null = null;
    let nonce = new Uint8Array();
    let started = false;
    let settled = false;
    // Fallback if the broker doesn't hand us ICE (older broker): the node runs coturn (STUN)
    // on :3478. STUN alone covers Wi-Fi / non-symmetric NAT; the broker's STUN+TURN adds cellular.
    const fallbackIce: RTCIceServer[] = [{ urls: `stun:${window.location.hostname}:3478` }];

    const cleanup = () => {
      clearTimeout(timer);
      clearTimeout(iceWait);
      try { ws.close(); } catch { /* ignore */ }
      try { pc?.close(); } catch { /* ignore */ }
    };
    const done = (err: Error | null, payload?: PairingPayload) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (err) reject(err);
      else resolve(payload as PairingPayload);
    };
    const timer = setTimeout(() => done(new Error("pairing timed out — check the code and your network")), timeoutMs);

    // Build the peer once we know which ICE servers to use, then offer. The broker hands a
    // pairing room the node's STUN+TURN so this works off-Wi-Fi too.
    const start = async (iceServers: RTCIceServer[]) => {
      if (started || settled) return;
      started = true;
      clearTimeout(iceWait);
      pc = new RTCPeerConnection({ iceServers });
      const channel = pc.createDataChannel("sb-pair");
      channel.onopen = () => {
        nonce = crypto.getRandomValues(new Uint8Array(16));
        channel.send(JSON.stringify({ type: "phello", nonce: bytesToB64(nonce) }));
      };
      channel.onmessage = async (ev) => {
        try {
          const m = JSON.parse(String(ev.data));
          if (m.type === "phello_ok") {
            const binding = await channelBinding(pc!.localDescription?.sdp ?? "", pc!.remoteDescription?.sdp ?? "");
            if (!macEqual(b64ToBytes(String(m.mac ?? "")), await mac(codeKey, "host", nonce, binding))) {
              return done(new Error("wrong code, or the connection couldn't be verified"));
            }
            const proof = await mac(codeKey, "guest", b64ToBytes(String(m.nonce2 ?? "")), binding);
            channel.send(JSON.stringify({ type: "pconfirm", mac: bytesToB64(proof) }));
          } else if (m.type === "ppayload") {
            done(null, parsePairingPayload(String(m.payload ?? "")));
          } else if (m.type === "perror") {
            done(new Error("incorrect code"));
          }
        } catch (e) {
          done(e instanceof Error ? e : new Error("pairing failed"));
        }
      };
      await pc.setLocalDescription(await pc.createOffer());
      await iceComplete(pc);
      ws.send(JSON.stringify({ type: "offer", sdp: pc.localDescription?.sdp ?? "" }));
    };
    const iceWait = setTimeout(() => start(fallbackIce), 1500); // broker silent -> STUN-only fallback

    ws.onopen = () => ws.send(JSON.stringify({ role: "phone", desktop_id: roomId }));
    ws.onerror = () => done(new Error("can't reach the pairing service"));
    ws.onmessage = async (ev) => {
      const m = JSON.parse(String(ev.data));
      if (m.type === "ice") {
        start(Array.isArray(m.iceServers) && m.iceServers.length ? (m.iceServers as RTCIceServer[]) : fallbackIce);
      } else if (m.type === "answer" && m.sdp && pc) {
        await pc.setRemoteDescription({ type: "answer", sdp: m.sdp });
      } else if (m.type === "error") {
        done(new Error(m.detail === "desktop offline" ? "no pairing in progress — start one on the Desktop" : "pairing failed"));
      }
    };
  });
}
