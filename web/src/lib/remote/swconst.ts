// Constants shared between the page-side remote bridge (sw-bridge.ts) and the service worker.

export const DIRECT_PARAM = "__direct"; // query marker: keep the LAN probe on the network, not the WebRTC relay
export const PAIRING_CACHE = "sb-pairing"; // CacheStorage namespace for the pairing (iOS 14+ shares it Safari<->PWA)
