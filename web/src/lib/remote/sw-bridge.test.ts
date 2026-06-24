// relayFetch is the single decision-and-execute point for the page-side /api -> WebRTC
// override. A bug here silently drops every relayed /api request (the original SW-based
// design failed this way: handshake worked, requests never reached the Desktop). Tests
// pin the routing rules: /api + /mcp go over the conn; non-/api paths and the __direct
// LAN probe fall through to real fetch; with no conn, everything falls through.

import { beforeEach, describe, expect, it, vi } from "vitest";

// The global setup file mocks $lib/remote/sw-bridge to a stub (so api.ts is testable
// without dragging in the Svelte rune). Here we DO want the real module, so unmock it.
// connection.svelte.ts uses $state which Node can't parse — mock its exports out so the
// sw-bridge import chain stays Node-clean.
vi.unmock("$lib/remote/sw-bridge");
vi.mock("./connection.svelte", () => ({
  remote: { status: "idle", detail: "", needsPairing: false },
  setRemoteStatus: vi.fn(),
  isConnected: () => false,
}));
vi.mock("./webrtc", () => ({
  startRemote: vi.fn(),
  getRemote: vi.fn(),
  stopRemote: vi.fn(),
}));
vi.mock("./store", () => ({ loadPairing: vi.fn(async () => null) }));

const { relayFetch } = await import("./sw-bridge");

const ORIGIN = "https://example.org";

type RecordedCall = { method: string; path: string; headers: Record<string, string>; body: Uint8Array };

function fakeConn(resp: { status: number; headers: Record<string, string>; body: Uint8Array }) {
  const calls: RecordedCall[] = [];
  return {
    calls,
    request: vi.fn(async (method: string, path: string, headers: Record<string, string>, body: Uint8Array) => {
      calls.push({ method, path, headers, body });
      return resp;
    }),
  };
}

const okResp = { status: 200, headers: { "x-relayed": "yes" }, body: new TextEncoder().encode("hi") };

let realFetchSpy: ReturnType<typeof vi.fn>;
beforeEach(() => {
  realFetchSpy = vi.fn(async () => new Response("real", { status: 299 }));
});

// In the browser, fetch("/api/...") works because Request resolves against the document
// base URL. Node's undici Request requires absolute URLs, so the tests pass absolute
// URLs (the production code happens to receive both forms — both encode the same intent).
const u = (p: string) => `${ORIGIN}${p}`;

describe("relayFetch", () => {
  it("routes /api requests over the relay (NOT the real fetch)", async () => {
    const conn = fakeConn(okResp);
    const res = await relayFetch(u("/api/health"), undefined, conn, realFetchSpy as unknown as typeof fetch, ORIGIN);
    expect(realFetchSpy).not.toHaveBeenCalled();
    expect(conn.request).toHaveBeenCalledTimes(1);
    expect(conn.calls[0].path).toBe("/api/health");
    expect(res.status).toBe(200);
    expect(res.headers.get("x-relayed")).toBe("yes");
    expect(await res.text()).toBe("hi");
  });

  it("routes /mcp requests over the relay too (same sensitivity as /api)", async () => {
    const conn = fakeConn(okResp);
    await relayFetch(u("/mcp/sse"), undefined, conn, realFetchSpy as unknown as typeof fetch, ORIGIN);
    expect(realFetchSpy).not.toHaveBeenCalled();
    expect(conn.calls[0].path).toBe("/mcp/sse");
  });

  it("falls through to real fetch when the URL has the __direct LAN-probe marker", async () => {
    const conn = fakeConn(okResp);
    await relayFetch(u("/api/health?__direct=1"), undefined, conn, realFetchSpy as unknown as typeof fetch, ORIGIN);
    expect(conn.request).not.toHaveBeenCalled();
    expect(realFetchSpy).toHaveBeenCalledTimes(1);
  });

  it("falls through to real fetch for non-/api paths (assets, html, etc.)", async () => {
    const conn = fakeConn(okResp);
    await relayFetch(u("/favicon.ico"), undefined, conn, realFetchSpy as unknown as typeof fetch, ORIGIN);
    await relayFetch(u("/"), undefined, conn, realFetchSpy as unknown as typeof fetch, ORIGIN);
    await relayFetch(u("/chat"), undefined, conn, realFetchSpy as unknown as typeof fetch, ORIGIN);
    expect(conn.request).not.toHaveBeenCalled();
    expect(realFetchSpy).toHaveBeenCalledTimes(3);
  });

  it("falls through to real fetch when there's no conn (LAN mode / not yet connected)", async () => {
    await relayFetch(u("/api/health"), undefined, null, realFetchSpy as unknown as typeof fetch, ORIGIN);
    expect(realFetchSpy).toHaveBeenCalledTimes(1);
  });

  it("forwards method + body bytes to the relay for non-GET requests", async () => {
    const conn = fakeConn(okResp);
    const body = JSON.stringify({ q: "hello" });
    await relayFetch(
      u("/api/chat"),
      { method: "POST", headers: { "content-type": "application/json" }, body },
      conn,
      realFetchSpy as unknown as typeof fetch,
      ORIGIN,
    );
    expect(conn.calls[0].method).toBe("POST");
    expect(conn.calls[0].headers["content-type"]).toBe("application/json");
    expect(new TextDecoder().decode(conn.calls[0].body)).toBe(body);
  });

  it("sends an empty body for GET/HEAD (Request.arrayBuffer() would throw on those)", async () => {
    const conn = fakeConn(okResp);
    await relayFetch(u("/api/health"), { method: "GET" }, conn, realFetchSpy as unknown as typeof fetch, ORIGIN);
    expect(conn.calls[0].body.byteLength).toBe(0);
  });

  it("returns a 503 'remote request failed' Response when the relay throws", async () => {
    const conn = {
      request: vi.fn(async () => {
        throw new Error("channel dead");
      }),
    };
    const res = await relayFetch(u("/api/health"), undefined, conn, realFetchSpy as unknown as typeof fetch, ORIGIN);
    expect(res.status).toBe(503);
    const data = await res.json();
    expect(data).toEqual({ detail: "remote request failed" });
  });

  it("accepts URL objects (the production override receives both string and URL inputs)", async () => {
    const conn = fakeConn(okResp);
    await relayFetch(new URL("/api/health?x=1", ORIGIN), undefined, conn, realFetchSpy as unknown as typeof fetch, ORIGIN);
    expect(conn.calls[0].path).toBe("/api/health?x=1");
  });
});
