import { getPublicKeyAsync, signAsync } from "@noble/ed25519";
import { describe, expect, it } from "vitest";

import { channelBinding, randomNonceB64, sdpFingerprint, verifyDesktopIdentity } from "./crypto";
import { decodePairingFragment, encodePairingFragment, parsePairingPayload } from "./pairing";
import { asResponse, b64ToBytes, bytesToB64, concatBytes, encodeRequest, parseMessage } from "./protocol";
import { classifyCandidatePair } from "./candidate-pair";

const SDP_A = "v=0\r\na=fingerprint:sha-256 AA:BB:CC\r\na=setup:actpass\r\n";
const SDP_B = "v=0\na=fingerprint:SHA-256 dd:ee:ff\na=setup:active\n";

function bytesToB64Node(b: Uint8Array): string {
  return Buffer.from(b).toString("base64");
}

describe("protocol framing", () => {
  it("round-trips base64 bytes (binary-safe)", () => {
    const raw = new Uint8Array([0, 255, 1, 254, 127]);
    expect([...b64ToBytes(bytesToB64(raw))]).toEqual([...raw]);
  });

  it("encodes a request frame the Python side can parse", () => {
    const text = encodeRequest("7", "POST", "/api/chat", { "content-type": "application/json" }, new TextEncoder().encode("{}"));
    const m = JSON.parse(text);
    expect(m).toMatchObject({ id: "7", method: "POST", path: "/api/chat" });
    expect(b64ToBytes(m.body_b64)).toEqual(new TextEncoder().encode("{}"));
  });

  it("parses a response frame into bytes", () => {
    const resp = asResponse(parseMessage(JSON.stringify({ id: "1", status: 200, headers: {}, body_b64: bytesToB64Node(new TextEncoder().encode("ok")) })));
    expect(resp.status).toBe(200);
    expect(new TextDecoder().decode(resp.body)).toBe("ok");
  });
});

describe("channel binding", () => {
  it("extracts + lower-cases the DTLS fingerprint", () => {
    expect(sdpFingerprint(SDP_A)).toBe("sha-256 aa:bb:cc");
    expect(sdpFingerprint(SDP_B)).toBe("sha-256 dd:ee:ff");
  });

  it("is order-independent (both peers compute the same value)", async () => {
    const ab = await channelBinding(SDP_A, SDP_B);
    const ba = await channelBinding(SDP_B, SDP_A);
    expect([...ab]).toEqual([...ba]);
  });

  it("differs for a different connection (relay would have different fingerprints)", async () => {
    const real = await channelBinding(SDP_A, SDP_B);
    const mitm = await channelBinding(SDP_A, "a=fingerprint:sha-256 99:88:77");
    expect([...real]).not.toEqual([...mitm]);
  });

  it("throws without both fingerprints", async () => {
    await expect(channelBinding(SDP_A, "v=0")).rejects.toThrow();
  });
});

describe("phone-side Desktop verification (closes the MITM prerequisite)", () => {
  it("accepts a signature over (nonce || binding) from the pinned key, and rejects forgeries", async () => {
    const priv = new Uint8Array(32).fill(7); // deterministic test key
    const pubB64 = bytesToB64Node(await getPublicKeyAsync(priv));
    const nonceB64 = randomNonceB64();
    const binding = await channelBinding(SDP_A, SDP_B);
    const sigB64 = bytesToB64Node(await signAsync(concatBytes(b64ToBytes(nonceB64), binding), priv));

    expect(await verifyDesktopIdentity(pubB64, nonceB64, binding, sigB64)).toBe(true);

    // relay-resistance: the SAME signature must NOT verify against a different binding...
    const otherBinding = await channelBinding(SDP_A, "a=fingerprint:sha-256 99:88:77");
    expect(await verifyDesktopIdentity(pubB64, nonceB64, otherBinding, sigB64)).toBe(false);
    // ...nor against a different pinned key (impostor)...
    const otherPub = bytesToB64Node(await getPublicKeyAsync(new Uint8Array(32).fill(9)));
    expect(await verifyDesktopIdentity(otherPub, nonceB64, binding, sigB64)).toBe(false);
    // ...nor with a replayed/altered nonce.
    expect(await verifyDesktopIdentity(pubB64, randomNonceB64(), binding, sigB64)).toBe(false);
    expect(await verifyDesktopIdentity(pubB64, nonceB64, binding, "garbage")).toBe(false);
  });
});

describe("pairing payload", () => {
  const payload = {
    v: 1, deviceId: "dev123", credential: "secret-cred", desktopPubkey: "pk",
    signalingUrl: "wss://connect.example.org", desktopId: "desk1", iceServers: [{ urls: "stun:x:3478" }],
  };

  it("round-trips through the QR fragment codec", () => {
    const got = decodePairingFragment("#" + encodePairingFragment(payload));
    expect(got).toEqual(payload);
  });

  it("rejects payloads missing required fields", () => {
    expect(() => parsePairingPayload(JSON.stringify({ deviceId: "x" }))).toThrow();
    expect(() => decodePairingFragment("#")).toThrow();
  });
});

describe("connectionKind classifies the nominated candidate pair", () => {
  // RTCStatsReport-shaped fixtures: id + type + nominated/state on the pair, candidateType
  // on the candidate rows. classifyCandidatePair must read BOTH local and remote types.
  const pair = (local: string, remote: string) => [
    { id: "pair1", type: "candidate-pair", nominated: true, localCandidateId: "L", remoteCandidateId: "R" },
    { id: "L", type: "local-candidate", candidateType: local },
    { id: "R", type: "remote-candidate", candidateType: remote },
  ];

  it("returns 'relay' when the REMOTE candidate is a relay (local srflx) — the bug we fixed", () => {
    expect(classifyCandidatePair(pair("srflx", "relay"))).toBe("relay");
  });

  it("returns 'relay' when the LOCAL candidate is a relay", () => {
    expect(classifyCandidatePair(pair("relay", "host"))).toBe("relay");
  });

  it("returns 'direct' only when neither side is a relay", () => {
    expect(classifyCandidatePair(pair("host", "host"))).toBe("direct");
    expect(classifyCandidatePair(pair("srflx", "srflx"))).toBe("direct");
  });

  it("returns 'unknown' when there's no nominated/succeeded pair", () => {
    expect(classifyCandidatePair([])).toBe("unknown");
  });
});

describe("protocol message guard (the request boundary)", () => {
  it("rejects a non-object JSON value (the historical null + primitive footguns)", () => {
    expect(() => parseMessage("null")).toThrow();
    expect(() => parseMessage("123")).toThrow();
    expect(() => parseMessage('"a string"')).toThrow();
  });

  it("rejects invalid JSON outright", () => {
    expect(() => parseMessage("{not json")).toThrow();
  });

  it("concatBytes preserves order and total length (request-encoding building block)", () => {
    const out = concatBytes(new Uint8Array([1, 2]), new Uint8Array([3, 4, 5]));
    expect([...out]).toEqual([1, 2, 3, 4, 5]);
  });
});
