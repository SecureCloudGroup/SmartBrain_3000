// Page-side glue for remote access. On boot it decides direct (LAN) vs remote mode.
// In remote mode it overrides window.fetch so the app's /api (+ /mcp) requests run
// through the page-held RemoteConnection (WebRTC DataChannel) instead of the network.
//
// Why intercept in the PAGE, not the service worker: iOS Safari's SW<->page relay is
// unreliable. Messages posted before the page's message queue is enabled are dropped
// (Safari has no postMessage buffering despite startMessages()); navigator.serviceWorker
// .controller is transiently null on a standalone-PWA launch; and iOS hard-kills the SW
// thread if a FetchEvent.respondWith() stays pending past ~70s. Together these silently
// dropped every relayed /api request (the DataChannel handshake worked, but requests
// never reached the Desktop). A page-level fetch override keeps the whole round-trip in
// the durable window context and works on iOS.

import { loadPairing } from "./store";
import type { PairingPayload } from "./pairing";
import { startRemote, getRemote, stopRemote } from "./webrtc";
import { DIRECT_PARAM } from "./swconst";
import { remote } from "./connection.svelte";

const _JSON = { "content-type": "application/json" };

// True if /api is reachable directly (on the Desktop LAN). The __direct marker keeps this
// probe on the network even after the fetch override is installed.
async function directReachable(): Promise<boolean> {
  try {
    const res = await fetch(`/api/health?${DIRECT_PARAM}=1`, { cache: "no-store", signal: AbortSignal.timeout(2000) });
    return res.ok;
  } catch {
    return false; // unreachable or timed out -> assume off-LAN, use the remote path
  }
}

// Same probe with a larger budget: retry a few times with a short warm-up delay. Used ONLY
// on the no-pairing decision, where a fresh Desktop whose backend is still cold-starting can
// fail the single 2s probe and get wrongly shown the phone "pair this device" welcome. A real
// off-LAN phone still (correctly) falls through after the extra few seconds.
async function directReachableWithRetry(): Promise<boolean> {
  for (let i = 0; i < 3; i++) {
    await new Promise((r) => setTimeout(r, 700));
    if (await directReachable()) return true;
  }
  return false;
}

// Minimal relay seam used by both installFetchRelay() (the production wiring) and the
// unit tests. The `conn` arg is the page-held RemoteConnection (or null); `realFetch` is
// the original window.fetch. Pure: no module-level reads, so it's testable in plain Node.
export type RelayConn = {
  request(method: string, path: string, headers: Record<string, string>, body: Uint8Array): Promise<{
    status: number; headers: Record<string, string>; body: Uint8Array;
  }>;
};
export async function relayFetch(
  input: RequestInfo | URL,
  init: RequestInit | undefined,
  conn: RelayConn | null,
  realFetch: typeof fetch,
  origin: string,
): Promise<Response> {
  const href = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const url = new URL(href, origin);
  const isApi = url.pathname.startsWith("/api") || url.pathname.startsWith("/mcp");
  if (!conn || !isApi || url.searchParams.has(DIRECT_PARAM)) return realFetch(input, init);
  const req = new Request(input as RequestInfo, init);
  const headers: Record<string, string> = {};
  req.headers.forEach((v, k) => (headers[k] = v));
  const body = ["GET", "HEAD"].includes(req.method)
    ? new Uint8Array()
    : new Uint8Array(await req.arrayBuffer());
  try {
    const resp = await conn.request(req.method, url.pathname + url.search, headers, body);
    return new Response(resp.body as BodyInit, { status: resp.status, headers: resp.headers });
  } catch {
    return new Response(JSON.stringify({ detail: "remote request failed" }), { status: 503, headers: _JSON });
  }
}

// Override window.fetch so /api (+ /mcp) go over the WebRTC DataChannel in remote mode.
// Everything else (and the __direct LAN probe) falls through to the real fetch unchanged.
function installFetchRelay(pairing: PairingPayload): void {
  const realFetch = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    // Reconnect-on-demand: if the retry budget burned out (terminal "offline"), any
    // /api call the user causes restarts the connection instead of failing until they
    // find a Retry button. request() awaits the fresh connection's ready, and its 60s
    // timeout comfortably covers the 15s connect budget.
    if (remote.status === "offline") {
      const href = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const p = new URL(href, window.location.origin).pathname;
      if (p.startsWith("/api") || p.startsWith("/mcp")) startRemote(pairing);
    }
    return relayFetch(input, init, getRemote(), realFetch, window.location.origin);
  };
}

let _markRemoteReady: () => void = () => {};
// Resolves once initRemote() has decided LAN-vs-remote and (in remote mode) installed the
// /api fetch override. api.ts awaits this before any /api call, so no request goes out on
// the native fetch before the relay is in place — child-page onMounts fire BEFORE the
// layout's onMount (which runs initRemote), so without this gate the first /api 404s.
export const remoteReady: Promise<void> = new Promise<void>((r) => {
  _markRemoteReady = r;
});

// Called once on app start (from the root layout). Resolves remoteReady when finished.
export async function initRemote(): Promise<void> {
  try {
    await _initRemote();
  } finally {
    _markRemoteReady();
  }
}

async function _initRemote(): Promise<void> {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
    return;
  }
  const pairing = await loadPairing().catch(() => null);
  if (!pairing) {
    // No stored pairing: retry the probe with a larger budget so a slow Desktop cold-start
    // isn't misread as off-LAN and shown the phone "pair this device" welcome.
    const onLan = (await directReachable()) || (await directReachableWithRetry());
    remote.needsPairing = !onLan;
    return;
  }
  if (await directReachable()) {
    return; // on the LAN — use /api directly, no relay
  }
  startRemote(pairing);
  installFetchRelay(pairing);
  // iOS Safari freezes JS when the page is backgrounded and does NOT release the
  // RTCPeerConnection, so the connection dies and a lingering dead PC blocks the next one
  // ("works once, then won't reconnect"). Tear down when the page is hidden, and
  // re-establish when it's shown again (return from background, bfcache restore, refocus).
  // Keep the connection alive across quick app-switches: on hide, wait a grace period
  // before tearing down — iOS doesn't freeze JS instantly and a live PC survives a brief
  // background, so a glance-and-return shouldn't churn the connection. Only a longer
  // background tears down (closing the PC before iOS strands it). pagehide (reload/navigate)
  // still tears down immediately — that's the original "can't reconnect after refresh" fix.
  // 3 minutes, not seconds: checking a message or camera and coming back was churning a
  // full reconnect every time. If iOS freezes JS before the timer fires, resume()'s
  // !isLive() check already handles the stranded PC, so a long grace costs nothing.
  const GRACE_MS = 180000;
  let hideTimer: ReturnType<typeof setTimeout> | undefined;
  const cancelHide = () => {
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = undefined;
  };
  const resume = () => {
    cancelHide();
    const conn = getRemote();
    // Reconnect if torn down, dropped, or the PC is silently dead (iOS froze before the
    // grace timer fired) — isLive() stops us showing "connected" over a corpse. This also
    // recovers from terminal "offline": startRemote mints a fresh RemoteConnection, so the
    // burned-out closed/reconnects state of the old one never lingers.
    if (!conn || remote.status === "offline" || remote.status === "reconnecting" || !conn.isLive()) {
      startRemote(pairing);
    }
  };
  window.addEventListener("pagehide", () => {
    cancelHide();
    stopRemote();
  });
  window.addEventListener("pageshow", resume); // bfcache restore
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      cancelHide();
      hideTimer = setTimeout(() => {
        hideTimer = undefined;
        stopRemote();
      }, GRACE_MS);
    } else {
      resume();
    }
  });
}

// Force iOS Safari to pick up a freshly deployed service worker. SvelteKit auto-registers
// the SW on window 'load'; we ask it to check for a new version now, and reload once the
// new SW takes control so the page's code never runs against a just-activated newer worker.
export function watchForSWUpdate(): void {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
  // Ask for a fresh service worker on load (Caddy serves it no-cache, so this hits the
  // network). New shell code is picked up on the next navigation — we deliberately do NOT
  // force a reload, which on iOS could flash a blank page mid-flow.
  navigator.serviceWorker.ready.then((reg) => reg.update()).catch(() => {});
}
