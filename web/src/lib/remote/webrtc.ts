// Page-held WebRTC connection to the Desktop. Lives in the window (NOT the service
// worker — iOS SWs can't host a live RTCPeerConnection). The service worker relays
// /api requests here (sw-bridge.ts); this module runs the channel-auth handshake and
// proxies each request over the DTLS-encrypted DataChannel.
//
// Handshake (see webrtc_peer.py): connect signaling -> offer/answer -> on channel open,
// send hello(nonce) -> verify the Desktop's signature against the PINNED pubkey + the
// channel binding (refuse on mismatch — possible MITM) -> only then send the credential.

import { channelBinding, randomNonceB64, verifyDesktopIdentity } from "./crypto";
import type { PairingPayload } from "./pairing";
import { appendChunk, asResponse, type ChunkState, encodeAuth, encodeHello, encodePing, encodeRequest, isChunkFrame, type ParsedResponse, parseMessage, pingDead } from "./protocol";
import { classifyCandidatePair } from "./candidate-pair";
import { setRemoteStatus } from "./connection.svelte";

const _ICE_GATHER_TIMEOUT = 3000;
const _REQUEST_TIMEOUT = 60000;
const _RECONNECT_BASE_MS = 1000; // capped exponential backoff + jitter (mirrors the Desktop loop)
const _RECONNECT_MAX_MS = 30000;
const _CONNECT_TIMEOUT_MS = 15000; // wall-clock per attempt: ICE can stall forever without this
// After this many failures, stop and tell the user (don't spin forever). 6 attempts of
// capped backoff ≈ 2.8 min of patience — enough for a phone radio waking from idle,
// still ≈ 7 broker registrations per episode (well under the broker's 30/60s limit).
const _MAX_RECONNECTS = 6;
// Keepalive over the DataChannel: phone pings, Desktop echoes a pong. Idle NAT mappings
// and consent timeouts silently kill the path after ~30s-2min of no traffic while
// connectionState keeps saying "connected" — steady pings keep the mapping warm, and a
// missing pong past the deadline reconnects proactively instead of at the next user tap.
const _PING_INTERVAL_MS = 20000;
const _PING_DEAD_MS = 45000; // > 2 intervals: one lost pong never kills a healthy link
// After registering, wait briefly for the broker to push ephemeral STUN/TURN before gathering ICE
// candidates; if none arrives (a static-mode node), fall back to the payload's ICE and offer anyway.
const _ICE_PUSH_WAIT_MS = 800;
// Shown when the connection can't be established. The #1 real-world cause (a VPN on the
// Desktop blocking the UDP relay path) is hard to detect, so we surface it as the first thing
// to try rather than spinning on "connecting…" indefinitely.
const _CONNECT_FAIL_HINT =
  "Couldn't reach your Desktop. If it's on a VPN, turning the VPN off on the Desktop often fixes this — then Retry.";

type Pending = {
  resolve: (r: ParsedResponse) => void;
  reject: (e: Error) => void;
  timer: ReturnType<typeof setTimeout>;
  chunks: ChunkState | null; // accumulating part-framed response, if the Desktop chunked
};

export class RemoteConnection {
  private pc: RTCPeerConnection | null = null;
  private ws: WebSocket | null = null;
  private channel: RTCDataChannel | null = null;
  private readonly pending = new Map<string, Pending>();
  private ready: Promise<void> | null = null;
  private markReady: (() => void) | null = null;
  private failReady: ((e: Error) => void) | null = null;
  private nonceB64 = "";
  private seq = 0;
  private closed = false;
  private reconnects = 0;
  private connectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private lastPong = 0; // ms epoch of the newest pong; 0 = no keepalive expected yet
  private offerSent = false; // one offer per attempt (the ICE push and the wait-timeout both trigger it)
  private iceWaitTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly pairing: PairingPayload) {}

  connect(): void {
    this.closed = false;
    this.offerSent = false;
    this.ready = new Promise<void>((resolve, reject) => {
      this.markReady = resolve;
      this.failReady = reject;
    });
    this.ready.catch(() => {}); // avoid unhandled-rejection noise; callers see status
    setRemoteStatus("connecting");
    // Bound the attempt: ICE can sit in "checking" forever (e.g. UDP blocked by a VPN) without
    // ever firing connectionState "failed", which would leave the UI on "connecting…" with no
    // way out. Treat a timeout exactly like a connection failure.
    this.connectTimer = setTimeout(() => this.onConnectFailure(), _CONNECT_TIMEOUT_MS);
    this.pc = new RTCPeerConnection({ iceServers: this.pairing.iceServers });
    this.pc.onconnectionstatechange = () => this.onPcState();
    this.channel = this.pc.createDataChannel("sb-api");
    this.channel.onopen = () => this.startHandshake();
    this.channel.onmessage = (e) => this.onChannelMessage(String(e.data));
    this.openSignaling();
  }

  private openSignaling(): void {
    const ws = new WebSocket(this.pairing.signalingUrl);
    this.ws = ws;
    ws.onopen = () => {
      ws.send(JSON.stringify({ role: "phone", desktop_id: this.pairing.desktopId }));
      // Register first; give the broker a moment to push ephemeral ICE before offering, so the
      // PC gathers relay candidates with fresh creds. Fall back to the payload ICE if none comes.
      this.iceWaitTimer = setTimeout(() => void this.makeOffer(), _ICE_PUSH_WAIT_MS);
    };
    ws.onerror = () => setRemoteStatus("offline", "can't reach the signaling service");
    ws.onmessage = (e) => this.onSignal(String(e.data));
  }

  private async makeOffer(): Promise<void> {
    if (!this.pc || this.offerSent) return; // once per attempt — ICE-push and the fallback timer both call here
    this.offerSent = true;
    if (this.iceWaitTimer) { clearTimeout(this.iceWaitTimer); this.iceWaitTimer = null; }
    await this.pc.setLocalDescription(await this.pc.createOffer());
    await this.iceGatheringDone();
    const sdp = this.pc.localDescription?.sdp ?? "";
    this.send({ type: "offer", sdp });
  }

  private iceGatheringDone(): Promise<void> {
    const pc = this.pc;
    if (!pc || pc.iceGatheringState === "complete") return Promise.resolve();
    return new Promise<void>((resolve) => {
      const done = () => {
        if (pc.iceGatheringState === "complete") finish();
      };
      const finish = () => {
        pc.removeEventListener("icegatheringstatechange", done);
        clearTimeout(t);
        resolve();
      };
      const t = setTimeout(finish, _ICE_GATHER_TIMEOUT); // some browsers stall; send what we have
      pc.addEventListener("icegatheringstatechange", done);
    });
  }

  private async onSignal(text: string): Promise<void> {
    let msg: { type?: string; sdp?: string; detail?: string; iceServers?: RTCIceServer[] };
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    if (msg.type === "ice" && Array.isArray(msg.iceServers)) {
      // Broker-pushed ephemeral STUN/TURN — apply to the not-yet-gathered PC, then offer. No TURN
      // secret is ever shipped in the app/QR; a leaked credential expires instead of being an open relay.
      if (this.pc && !this.offerSent) {
        try { this.pc.setConfiguration({ iceServers: msg.iceServers }); } catch { /* unsupported -> payload ICE */ }
      }
      void this.makeOffer();
    } else if (msg.type === "answer" && msg.sdp && this.pc) {
      await this.pc.setRemoteDescription({ type: "answer", sdp: msg.sdp });
    } else if (msg.type === "error") {
      setRemoteStatus("offline", msg.detail || "the Desktop isn't reachable");
    }
  }

  private startHandshake(): void {
    this.nonceB64 = randomNonceB64();
    this.send(undefined, encodeHello(this.nonceB64)); // first channel message: challenge the Desktop
  }

  private async onChannelMessage(text: string): Promise<void> {
    let m: Record<string, unknown>;
    try {
      m = parseMessage(text);
    } catch {
      return;
    }
    const type = m.type as string | undefined;
    if (type === "hello_ok") return this.onHelloOk(m);
    if (type === "auth_ok") return this.onAuthOk();
    if (type === "pong") {
      this.lastPong = Date.now();
      return;
    }
    if (type === "auth_error") {
      setRemoteStatus("offline", "this device isn't authorized — re-pair");
      this.failReady?.(new Error("auth_error"));
      return;
    }
    if (isChunkFrame(m)) return this.onChunkFrame(m); // one part of a big response
    if (typeof m.status === "number") this.resolvePending(asResponse(m)); // a response frame
  }

  private async onHelloOk(m: Record<string, unknown>): Promise<void> {
    setRemoteStatus("verifying");
    const localSdp = this.pc?.localDescription?.sdp ?? "";
    const remoteSdp = this.pc?.remoteDescription?.sdp ?? "";
    let ok = false;
    try {
      const binding = await channelBinding(localSdp, remoteSdp);
      ok = await verifyDesktopIdentity(this.pairing.desktopPubkey, this.nonceB64, binding, String(m.signature ?? ""));
    } catch {
      ok = false;
    }
    if (!ok || m.pubkey !== this.pairing.desktopPubkey) {
      // The Desktop could NOT be verified — refuse and send NOTHING (possible MITM).
      setRemoteStatus("untrusted", "couldn't verify your Desktop — connection blocked");
      this.failReady?.(new Error("untrusted"));
      this.close();
      return;
    }
    this.send(undefined, encodeAuth(this.pairing.deviceId, this.pairing.credential)); // proven: safe to auth
  }

  private async onAuthOk(): Promise<void> {
    if (this.connectTimer) { clearTimeout(this.connectTimer); this.connectTimer = null; } // connected in time
    this.lastPong = Date.now(); // the auth round-trip proves the path is alive right now
    this.pingTimer = setInterval(() => this.pingTick(), _PING_INTERVAL_MS);
    const kind = await this.connectionKind();
    // Fail SAFE on uncertainty: don't claim "direct (P2P)" unless we actually confirmed it —
    // a relayed session mislabeled as direct would falsely reassure the user.
    setRemoteStatus(kind === "relay" ? "connected-relay" : kind === "direct" ? "connected-direct" : "connected");
    this.markReady?.();
  }

  private async connectionKind(): Promise<"direct" | "relay" | "unknown"> {
    if (!this.pc) return "unknown";
    try {
      return classifyCandidatePair(await this.pc.getStats());
    } catch {
      return "unknown";
    }
  }

  // Keepalive tick: declare the connection dead if pongs stopped, otherwise ping.
  // The dead-path case matters more than the traffic: an expired NAT mapping leaves
  // connectionState on "connected" for minutes, so without this the user's next tap
  // is what discovers the corpse (and eats the 60s request timeout).
  private pingTick(): void {
    if (this.closed || this.channel?.readyState !== "open") return;
    if (pingDead(this.lastPong, Date.now(), _PING_DEAD_MS)) {
      this.onConnectFailure();
      return;
    }
    this.send(undefined, encodePing(Date.now()));
  }

  private onPcState(): void {
    const s = this.pc?.connectionState;
    if (this.closed) return;
    if (s === "connected") this.reconnects = 0; // healthy — reset backoff
    if (s === "failed" || s === "disconnected") this.onConnectFailure();
  }

  // One path for every way an attempt can fail (pc "failed"/"disconnected" OR the connect
  // timeout firing): retry with backoff a bounded number of times, then give up and surface a
  // clear, actionable message instead of looping on "connecting…"/"reconnecting…" forever.
  private onConnectFailure(): void {
    if (this.closed) return;
    this.teardown(); // also clears the connect timer
    if (this.reconnects >= _MAX_RECONNECTS) {
      this.failTerminal();
      return;
    }
    setRemoteStatus("reconnecting");
    const ceiling = Math.min(_RECONNECT_MAX_MS, _RECONNECT_BASE_MS * 2 ** this.reconnects);
    const delay = ceiling / 2 + Math.random() * (ceiling / 2); // capped exponential + jitter
    this.reconnects += 1;
    setTimeout(() => {
      if (!this.closed) this.connect();
    }, delay);
  }

  private failTerminal(): void {
    this.closed = true; // stop the retry loop until the user (or a resume) restarts it
    this.teardown();
    setRemoteStatus("offline", _CONNECT_FAIL_HINT);
  }

  async request(method: string, path: string, headers: Record<string, string>, body: Uint8Array): Promise<ParsedResponse> {
    if (!this.ready) throw new Error("not connecting");
    await this.ready;
    const id = String(++this.seq);
    const frame = encodeRequest(id, method, path, headers, body);
    return new Promise<ParsedResponse>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("remote request timed out"));
      }, _REQUEST_TIMEOUT);
      this.pending.set(id, { resolve, reject, timer, chunks: null });
      try {
        this.channel?.send(frame);
      } catch (e) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(e instanceof Error ? e : new Error("send failed"));
      }
    });
  }

  // Is the underlying PeerConnection still usable? Used by the page's resume handler to
  // avoid showing "connected" over a corpse when iOS froze JS before teardown ran.
  isLive(): boolean {
    const s = this.pc?.connectionState;
    return s === "new" || s === "connecting" || s === "connected";
  }

  // A big response arrives as ordered part-frames (see protocol.ts). Each part refreshes
  // the request timer — the 60s budget then bounds the GAP between parts, not the whole
  // transfer, so a slow relayed link can still deliver a large document.
  private onChunkFrame(m: Record<string, unknown>): void {
    const id = String(m.id ?? "");
    const p = this.pending.get(id);
    if (!p) return; // request already timed out / cancelled — drop the stragglers
    const r = appendChunk(p.chunks, m);
    if (r.error) {
      clearTimeout(p.timer);
      this.pending.delete(id);
      p.reject(new Error(r.error));
      return;
    }
    if (r.done) {
      this.resolvePending(r.done);
      return;
    }
    p.chunks = r.state ?? null;
    clearTimeout(p.timer);
    p.timer = setTimeout(() => {
      this.pending.delete(id);
      p.reject(new Error("remote request timed out"));
    }, _REQUEST_TIMEOUT);
  }

  private resolvePending(resp: ParsedResponse): void {
    const p = this.pending.get(resp.id);
    if (!p) return;
    clearTimeout(p.timer);
    this.pending.delete(resp.id);
    p.resolve(resp);
  }

  private send(obj?: object, raw?: string): void {
    const text = raw ?? JSON.stringify(obj);
    // signaling messages go on the WS; channel messages are sent by their callers
    if (raw && this.channel?.readyState === "open") this.channel.send(raw);
    else if (obj) this.ws?.send(text);
  }

  private teardown(): void {
    if (this.connectTimer) { clearTimeout(this.connectTimer); this.connectTimer = null; }
    if (this.iceWaitTimer) { clearTimeout(this.iceWaitTimer); this.iceWaitTimer = null; }
    if (this.pingTimer) { clearInterval(this.pingTimer); this.pingTimer = null; }
    this.lastPong = 0; // next connection starts a fresh keepalive clock (set on auth_ok)
    for (const [, p] of this.pending) {
      clearTimeout(p.timer); // fail in-flight requests fast instead of hanging to the timeout
      p.reject(new Error("connection dropped"));
    }
    this.pending.clear();
    try {
      this.ws?.close();
    } catch { /* ignore */ }
    try {
      this.pc?.close();
    } catch { /* ignore */ }
    this.ws = this.pc = this.channel = null;
  }

  close(): void {
    this.closed = true;
    this.teardown();
  }
}

let current: RemoteConnection | null = null;

export function startRemote(pairing: PairingPayload): RemoteConnection {
  current?.close();
  current = new RemoteConnection(pairing);
  current.connect();
  return current;
}

// Tear the connection down explicitly. iOS Safari does NOT release the
// RTCPeerConnection when the page unloads/reloads, and the lingering session
// blocks the next one (works once, then a refresh/relaunch can't connect) —
// so callers wire this to `pagehide`.
export function stopRemote(): void {
  current?.close();
  current = null;
}

export function getRemote(): RemoteConnection | null {
  return current;
}
