import { getPublicKeyAsync, signAsync } from "@noble/ed25519";
import { describe, expect, it } from "vitest";

import { channelBinding, randomNonceB64, sdpFingerprint, verifyDesktopIdentity } from "./crypto";
import { decodePairingFragment, encodePairingFragment, parsePairingPayload } from "./pairing";
import { appendChunk, asResponse, b64ToBytes, bytesToB64, concatBytes, encodePing, encodeRequest, isChunkFrame, parseMessage, pingDead } from "./protocol";
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

describe("keepalive (ping/pong)", () => {
  it("encodePing carries the timestamp in the documented frame shape", () => {
    expect(JSON.parse(encodePing(12345))).toEqual({ type: "ping", t: 12345 });
  });

  it("pingDead: never dead before the first pong (lastPong 0)", () => {
    expect(pingDead(0, 1_000_000, 45_000)).toBe(false);
  });

  it("pingDead: alive within the deadline, dead past it", () => {
    const now = 1_000_000;
    expect(pingDead(now - 44_999, now, 45_000)).toBe(false);
    expect(pingDead(now - 45_000, now, 45_000)).toBe(false); // boundary: exactly at deadline
    expect(pingDead(now - 45_001, now, 45_000)).toBe(true);
  });
});

describe("chunked responses (big audit feeds / large documents)", () => {
  it("encodeRequest advertises chunk support", () => {
    const m = JSON.parse(encodeRequest("1", "GET", "/api/audit", {}, new Uint8Array()));
    expect(m.chunks).toBe(true);
  });

  it("reassembles ordered parts into one response", () => {
    const body = "A".repeat(10);
    const b64 = Buffer.from(body).toString("base64"); // 16 chars
    const p1 = { id: "5", seq: 0, more: true, status: 200, headers: { "content-type": "application/json" }, body_b64: b64.slice(0, 8) };
    const p2 = { id: "5", seq: 1, more: false, body_b64: b64.slice(8) };
    const r1 = appendChunk(null, p1);
    expect(r1.state && !r1.done && !r1.error).toBe(true);
    const r2 = appendChunk(r1.state!, p2);
    expect(r2.done?.status).toBe(200);
    expect(r2.done?.headers["content-type"]).toBe("application/json");
    expect(new TextDecoder().decode(r2.done!.body)).toBe(body);
  });

  it("a single-part stream (more:false at seq 0) completes immediately", () => {
    const r = appendChunk(null, { id: "6", seq: 0, more: false, status: 204, headers: {}, body_b64: "" });
    expect(r.done?.status).toBe(204);
    expect(r.done?.body.length).toBe(0);
  });

  it("rejects a stream that doesn't start at seq 0 with a status", () => {
    expect(appendChunk(null, { id: "7", seq: 1, more: false, body_b64: "" }).error).toBeTruthy();
    expect(appendChunk(null, { id: "7", seq: 0, more: true, body_b64: "" }).error).toBeTruthy(); // no status
  });

  it("rejects an out-of-order part", () => {
    const r1 = appendChunk(null, { id: "8", seq: 0, more: true, status: 200, headers: {}, body_b64: "AA" });
    expect(appendChunk(r1.state!, { id: "8", seq: 2, more: false, body_b64: "BB" }).error).toBeTruthy();
  });

  it("bounds total accumulation", () => {
    const big = "A".repeat(7 * 1024 * 1024);
    const r1 = appendChunk(null, { id: "9", seq: 0, more: true, status: 200, headers: {}, body_b64: big });
    expect(r1.error).toBeUndefined();
    expect(appendChunk(r1.state!, { id: "9", seq: 1, more: true, body_b64: big }).error).toBeTruthy();
  });

  it("isChunkFrame separates parts from plain responses and control messages", () => {
    expect(isChunkFrame({ id: "1", seq: 0, more: false })).toBe(true);
    expect(isChunkFrame({ id: "1", status: 200, body_b64: "" })).toBe(false);
    expect(isChunkFrame({ type: "pong", t: 1 })).toBe(false);
  });
});
