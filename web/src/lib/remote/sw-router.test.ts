// Pins the service-worker fetch decision tree. Two invariants get explicit tests:
//   1. /api + /mcp are NEVER cache-first (would leak decrypted secrets at rest).
//   2. A navigation to /api is "passthrough" — the Gmail OAuth callback regressed once
//      because the SW tried to re-fetch a cross-origin-initiated navigation and the
//      browser failed it with ERR_FAILED.

import { describe, expect, it } from "vitest";

import { swFetchAction } from "./sw-router";

describe("swFetchAction", () => {
  it("returns 'passthrough' for a top-level navigation to /api (the OAuth callback path)", () => {
    expect(swFetchAction("/api/email/callback", "GET", "navigate")).toBe("passthrough");
    expect(swFetchAction("/mcp/anything", "GET", "navigate")).toBe("passthrough");
  });

  it("returns 'network-only' for non-navigation /api + /mcp requests", () => {
    expect(swFetchAction("/api/health", "GET", "cors")).toBe("network-only");
    expect(swFetchAction("/api/chat", "POST", "cors")).toBe("network-only");
    expect(swFetchAction("/mcp/sse", "GET", "no-cors")).toBe("network-only");
  });

  it("NEVER serves /api or /mcp from the cache (the at-rest secrets invariant)", () => {
    // No combination of method/mode should return cache-first for these prefixes.
    const methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"];
    const modes = ["cors", "no-cors", "same-origin", "navigate"];
    const paths = ["/api", "/api/x", "/mcp", "/mcp/sse"];
    for (const p of paths) {
      for (const m of methods) {
        for (const mode of modes) {
          expect(swFetchAction(p, m, mode)).not.toBe("cache-first");
        }
      }
    }
  });

  it("returns 'passthrough' for non-GET, non-/api requests (the SW doesn't intercept writes)", () => {
    expect(swFetchAction("/", "POST", "cors")).toBe("passthrough");
    expect(swFetchAction("/anything", "PUT", "cors")).toBe("passthrough");
  });

  it("returns 'navigate-network-first' for SPA navigations to non-/api routes", () => {
    expect(swFetchAction("/chat", "GET", "navigate")).toBe("navigate-network-first");
    expect(swFetchAction("/", "GET", "navigate")).toBe("navigate-network-first");
  });

  it("returns 'cache-first' for GETs to static/built assets", () => {
    expect(swFetchAction("/_app/immutable/chunks/x.js", "GET", "no-cors")).toBe("cache-first");
    expect(swFetchAction("/favicon.ico", "GET", "no-cors")).toBe("cache-first");
  });
});
