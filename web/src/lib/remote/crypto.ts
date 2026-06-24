// Channel authentication on the PHONE side — the half that closes the MITM prerequisite.
//
// Before sending its credential, the phone challenges the Desktop with a nonce; the
// Desktop replies with Ed25519(nonce || channel_binding). The phone recomputes the same
// channel_binding from THIS connection's DTLS fingerprints and verifies the signature
// against the public key it pinned at pairing. Because a relaying MITM's two DTLS legs
// carry different fingerprints, a signature it forwards from the real Desktop is bound to
// the wrong channel and fails here — so the phone never sends its credential to an impostor.
//
// Must stay byte-for-byte compatible with app/smartbrain_3000/webrtc_peer.channel_binding:
// fingerprints are lower-cased, sorted, joined with "|", then SHA-256'd.

import { verifyAsync } from "@noble/ed25519";

import { b64ToBytes, concatBytes } from "./protocol";

export function sdpFingerprint(sdp: string): string {
  for (const line of (sdp || "").split(/\r?\n/)) {
    if (line.startsWith("a=fingerprint:")) {
      return line.slice(line.indexOf(":") + 1).trim().toLowerCase();
    }
  }
  return "";
}

export async function channelBinding(localSdp: string, remoteSdp: string): Promise<Uint8Array> {
  const local = sdpFingerprint(localSdp);
  const remote = sdpFingerprint(remoteSdp);
  if (!local || !remote) throw new Error("channel binding needs both DTLS fingerprints");
  const pair = [local, remote].sort().join("|");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(pair));
  return new Uint8Array(digest);
}

// Verify the Desktop's hello_ok over (nonce || binding) against the pinned public key.
// Returns false (never throws) on any malformed input or signature mismatch.
export async function verifyDesktopIdentity(
  pinnedPubkeyB64: string,
  nonceB64: string,
  binding: Uint8Array,
  signatureB64: string,
): Promise<boolean> {
  try {
    const message = concatBytes(b64ToBytes(nonceB64), binding);
    return await verifyAsync(b64ToBytes(signatureB64), message, b64ToBytes(pinnedPubkeyB64));
  } catch {
    return false;
  }
}

export function randomNonceB64(): string {
  const n = new Uint8Array(16);
  crypto.getRandomValues(n);
  let s = "";
  for (const b of n) s += String.fromCharCode(b);
  return btoa(s);
}
