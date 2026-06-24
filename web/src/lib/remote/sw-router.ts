// Pure routing predicate for the service worker's fetch handler. Splitting this out so the
// SW's invariants are unit-testable (the SW module itself imports $service-worker which is
// only resolvable inside a SvelteKit build).
//
// INVARIANT 1: /api and /mcp are NEVER served from the cache (they carry decrypted secrets).
// INVARIANT 2: a top-level NAVIGATION to /api (e.g. the Gmail OAuth callback) must be
//              "passthrough" — re-fetching a cross-origin-initiated navigation request fails
//              in some browsers (ERR_FAILED). The browser handles it natively.

export type SwAction =
  | "passthrough" // let the browser handle it (no respondWith)
  | "network-only" // fetch the request directly, never the cache
  | "navigate-network-first" // try network, fall back to the cached shell
  | "cache-first"; // serve from cache, fall back to network

export function swFetchAction(pathname: string, method: string, mode: string): SwAction {
  const isSensitive = pathname.startsWith("/api") || pathname.startsWith("/mcp");
  if (isSensitive) {
    if (mode === "navigate") return "passthrough"; // OAuth callback path
    return "network-only";
  }
  if (method !== "GET") return "passthrough";
  if (mode === "navigate") return "navigate-network-first";
  return "cache-first";
}
