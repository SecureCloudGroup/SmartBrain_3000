/// <reference types="@sveltejs/kit" />
/// <reference lib="webworker" />
//
// SmartBrain PWA service worker (built by SvelteKit so the precache list tracks
// the hashed bundles).
//
// SECURITY INVARIANT: never cache /api or /mcp responses. They carry decrypted
// secrets and knowledge-base content; caching them would persist sensitive data
// in CacheStorage and defeat the at-rest encryption model. Only the static app
// shell (bundles + static files + "/") is precached for offline loads.

import { build, files, version } from "$service-worker";

import { swFetchAction } from "$lib/remote/sw-router";
import { PAIRING_CACHE } from "$lib/remote/swconst";

const CACHE = `smartbrain-${version}`;
const SHELL = [...build, ...files, "/"];

// Re-type the worker global without an `unknown` escape hatch (SW event types).
declare const self: ServiceWorkerGlobalScope;
const sw = self;

sw.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => sw.skipWaiting()),
  );
});

sw.addEventListener("activate", (event) => {
  // Drop stale shell caches, but KEEP the pairing cache (it's not a shell cache).
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE && k !== PAIRING_CACHE).map((k) => caches.delete(k))),
      )
      .then(() => sw.clients.claim()),
  );
});

sw.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const action = swFetchAction(url.pathname, event.request.method, event.request.mode);
  if (action === "passthrough") return;
  if (action === "network-only") {
    // /api + /mcp: always network, NEVER cached. Off the LAN the page's fetch override
    // (sw-bridge.ts) routes these over WebRTC before reaching the network.
    event.respondWith(fetch(event.request));
    return;
  }
  if (action === "navigate-network-first") {
    // Navigations: network-first, so a rebuilt shell (new hashed-asset links) is
    // never served stale. Fall back to the cached shell only when offline.
    event.respondWith(
      fetch(event.request).catch(
        () => caches.match(event.request).then((hit) => hit || caches.match("/")) as Promise<Response>,
      ),
    );
    return;
  }
  // cache-first: hashed, immutable build assets + static files.
  event.respondWith(caches.match(event.request).then((hit) => hit || fetch(event.request)));
});
