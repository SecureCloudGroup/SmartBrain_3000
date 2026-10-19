// request() awaits the ready promise of the connect attempt current at call time. Every
// attempt mints a new promise, so one that ends without settling its own (connect
// timeout, ICE failure, close/stopRemote) left those requests hanging forever: the 60s
// request timer only starts after ready, so relayFetch's 503 was never reached and the
// UI sat on "Loading…". These tests pin: an attempt's waiters always settle when it ends,
// and a stale attempt can never resolve the next attempt's promise.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./connection.svelte", () => ({ setRemoteStatus: vi.fn() }));
vi.mock("./crypto", () => ({
  channelBinding: vi.fn(async () => "binding"),
  randomNonceB64: () => "nonce",
  verifyDesktopIdentity: vi.fn(async () => true),
}));
vi.mock("./candidate-pair", () => ({ classifyCandidatePair: () => "direct" }));

class FakeChannel {
  readyState = "connecting";
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  send(s: string): void {
    this.sent.push(s);
  }
  deliver(m: object): void {
    this.onmessage?.({ data: JSON.stringify(m) });
  }
}

class FakePC {
  static all: FakePC[] = [];
  connectionState = "new";
  iceGatheringState = "complete";
  localDescription = { sdp: "local" };
  remoteDescription = { sdp: "remote" };
  onconnectionstatechange: (() => void) | null = null;
  channel = new FakeChannel();
  stats: Promise<unknown> = Promise.resolve(new Map());
  constructor() {
    FakePC.all.push(this);
  }
  createDataChannel(): FakeChannel {
    return this.channel;
  }
  getStats(): Promise<unknown> {
    return this.stats;
  }
  close(): void {
    this.connectionState = "closed";
  }
}

class FakeWS {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: (() => void) | null = null;
  send(): void {}
  close(): void {}
}

const PAIRING = {
  v: 1, deviceId: "dev", credential: "cred", desktopPubkey: "pk",
  signalingUrl: "wss://broker.example", desktopId: "desk", iceServers: [],
};

// Drive one attempt's channel through hello -> hello_ok -> auth_ok (signaling is bypassed).
async function handshake(pc: FakePC): Promise<void> {
  pc.channel.readyState = "open";
  pc.channel.onopen?.();
  pc.channel.deliver({ type: "hello_ok", pubkey: "pk", signature: "sig" });
  await vi.advanceTimersByTimeAsync(0);
  pc.channel.deliver({ type: "auth_ok" });
  await vi.advanceTimersByTimeAsync(0);
}

// Track settlement without awaiting (a hang must show up as "pending", not a test timeout).
function track<T>(p: Promise<T>): { state: string } {
  const s = { state: "pending" };
  p.then(() => (s.state = "resolved"), () => (s.state = "rejected"));
  return s;
}

const { RemoteConnection } = await import("./webrtc");
const { setRemoteStatus } = await import("./connection.svelte");
const { verifyDesktopIdentity } = await import("./crypto");

beforeEach(() => {
  vi.useFakeTimers();
  FakePC.all = [];
  vi.stubGlobal("RTCPeerConnection", FakePC);
  vi.stubGlobal("WebSocket", FakeWS);
  vi.mocked(setRemoteStatus).mockClear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("RemoteConnection ready promise", () => {
  it("fails a request waiting on an attempt that timed out, then serves the retry", async () => {
    const conn = new RemoteConnection(PAIRING);
    conn.connect();
    const first = track(conn.request("GET", "/api/health", {}, new Uint8Array()));
    await vi.advanceTimersByTimeAsync(15000); // connect timeout -> teardown + backoff
    expect(first.state).toBe("rejected");
    await vi.advanceTimersByTimeAsync(1000); // backoff (<= 1s on the first retry) -> attempt 2
    expect(FakePC.all.length).toBe(2);
    await handshake(FakePC.all[1]);
    const second = track(conn.request("GET", "/api/health", {}, new Uint8Array()));
    await vi.advanceTimersByTimeAsync(0);
    const sent = JSON.parse(FakePC.all[1].channel.sent.at(-1) ?? "{}");
    expect(sent.path).toBe("/api/health");
    FakePC.all[1].channel.deliver({ id: sent.id, status: 200, headers: {}, body_b64: "" });
    await vi.advanceTimersByTimeAsync(0);
    expect(second.state).toBe("resolved");
    conn.close();
  });

  it("fails a waiting request when the connection is closed (stopRemote)", async () => {
    const conn = new RemoteConnection(PAIRING);
    conn.connect();
    const waiting = track(conn.request("GET", "/api/health", {}, new Uint8Array()));
    conn.close();
    await vi.advanceTimersByTimeAsync(0);
    expect(waiting.state).toBe("rejected");
  });

  it("a stale attempt's late auth_ok cannot mark the next attempt ready", async () => {
    const conn = new RemoteConnection(PAIRING);
    conn.connect();
    const pc1 = FakePC.all[0];
    let releaseStats: (v: unknown) => void = () => {};
    pc1.stats = new Promise((r) => (releaseStats = r)); // hold onAuthOk inside connectionKind()
    pc1.channel.readyState = "open";
    pc1.channel.onopen?.();
    pc1.channel.deliver({ type: "hello_ok", pubkey: "pk", signature: "sig" });
    await vi.advanceTimersByTimeAsync(0);
    pc1.channel.deliver({ type: "auth_ok" });
    pc1.connectionState = "failed";
    pc1.onconnectionstatechange?.(); // attempt 1 dies mid-auth_ok
    await vi.advanceTimersByTimeAsync(1000); // attempt 2 starts
    expect(FakePC.all.length).toBe(2);
    const waiting = track(conn.request("GET", "/api/health", {}, new Uint8Array()));
    releaseStats(new Map()); // attempt 1's onAuthOk now finishes
    await vi.advanceTimersByTimeAsync(0);
    expect(waiting.state).toBe("pending"); // attempt 2 has not authenticated yet
    expect(FakePC.all[1].channel.sent.length).toBe(0);
    expect(setRemoteStatus).not.toHaveBeenCalledWith("connected-direct"); // no stale "connected" chip
    conn.close();
  });

  it("fails a request made in the backoff window after a live connection drops", async () => {
    const conn = new RemoteConnection(PAIRING);
    conn.connect();
    await handshake(FakePC.all[0]); // live session
    FakePC.all[0].connectionState = "failed";
    FakePC.all[0].onconnectionstatechange?.(); // drop -> teardown + backoff
    const during = track(conn.request("GET", "/api/health", {}, new Uint8Array()));
    await vi.advanceTimersByTimeAsync(0);
    expect(during.state).toBe("rejected"); // not a 60s hang on a dead channel
    conn.close();
  });

  it("a stale attempt's late hello_ok verification cannot verify or authenticate the next attempt", async () => {
    const conn = new RemoteConnection(PAIRING);
    conn.connect();
    const pc1 = FakePC.all[0];
    let releaseVerify: (v: boolean) => void = () => {};
    vi.mocked(verifyDesktopIdentity).mockImplementationOnce(() => new Promise((r) => (releaseVerify = r)));
    pc1.channel.readyState = "open";
    pc1.channel.onopen?.();
    pc1.channel.deliver({ type: "hello_ok", pubkey: "pk", signature: "sig" }); // held in verification
    await vi.advanceTimersByTimeAsync(0);
    pc1.connectionState = "failed";
    pc1.onconnectionstatechange?.(); // attempt 1 dies mid-verification
    await vi.advanceTimersByTimeAsync(1000); // attempt 2 starts
    const pc2 = FakePC.all[1];
    pc2.channel.readyState = "open";
    releaseVerify(true); // attempt 1's onHelloOk now finishes
    await vi.advanceTimersByTimeAsync(0);
    expect(pc2.channel.sent.some((f) => JSON.parse(f).type === "auth")).toBe(false); // no credential on attempt 2
    const waiting = track(conn.request("GET", "/api/health", {}, new Uint8Array()));
    pc2.channel.deliver({ type: "auth_ok" }); // an unverified peer skipping hello_ok
    await vi.advanceTimersByTimeAsync(0);
    expect(waiting.state).toBe("pending"); // attempt 2 never proved identity, so it isn't ready
    conn.close();
  });
});
