// The req<T> wrapper in api.ts is the choke-point for every typed call. The contract:
// non-2xx -> throw ApiError(status, detail-from-server); 423 also triggers a /unlock
// navigation; 2xx -> resolve to parsed JSON. Tests exercise the wrapper via api.health()
// because req itself is module-internal.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// $app/navigation + $lib/remote/sw-bridge are stubbed in vitest-setup.ts. The setup
// installs goto as vi.fn(); we read it here to assert the 423 -> /unlock side-effect.
const { goto } = await import("$app/navigation");
const gotoSpy = goto as unknown as ReturnType<typeof vi.fn>;
const { api, ApiError } = await import("./api");

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

let realFetch: typeof globalThis.fetch;
beforeEach(() => {
  realFetch = globalThis.fetch;
  gotoSpy.mockReset();
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

describe("req wrapper (via api.health)", () => {
  it("resolves to parsed JSON on a 2xx", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse(200, { status: "ok", version: "test" }),
    ) as unknown as typeof globalThis.fetch;
    await expect(api.health()).resolves.toEqual({ status: "ok", version: "test" });
  });

  it("throws ApiError with the response status and server detail on non-2xx", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse(400, { detail: "title is required" }),
    ) as unknown as typeof globalThis.fetch;
    await expect(api.health()).rejects.toMatchObject({
      name: "ApiError",
      status: 400,
      message: "title is required",
    });
  });

  it("falls back to a generic detail when the server didn't return one", async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response("not json", { status: 500 }),
    ) as unknown as typeof globalThis.fetch;
    const err = await api.health().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as { status: number }).status).toBe(500);
    expect((err as { message: string }).message).toBe("request failed (500)");
  });

  it("navigates to /unlock on 423 AND throws (so callers stop their flow)", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse(423, { detail: "locked" }),
    ) as unknown as typeof globalThis.fetch;
    await expect(api.health()).rejects.toMatchObject({ status: 423 });
    expect(gotoSpy).toHaveBeenCalledWith("/unlock");
  });

  it("does NOT navigate on non-423 errors", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse(503, { detail: "down" }),
    ) as unknown as typeof globalThis.fetch;
    await api.health().catch(() => null);
    expect(gotoSpy).not.toHaveBeenCalled();
  });
});

// Vault calls that do NOT go through req<T>: export/import hand-roll fetch (a Blob body, a raw
// upload), so they carry their own headers and their own error path. Both are worth pinning —
// a dropped x-sb-local header is a 403, and a dropped `vault` param silently searches EVERYTHING
// instead of the one vault the user scoped to.
describe("vault client calls", () => {
  // Typed params, so `mock.calls` is a real tuple rather than [] and the assertions below type-check.
  function captureFetch(status = 200, body: unknown = {}) {
    const spy = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse(status, body),
    );
    globalThis.fetch = spy as unknown as typeof globalThis.fetch;
    return spy;
  }

  it("omits `vault` from a search when nothing is scoped", async () => {
    const spy = captureFetch(200, { results: [] });
    await api.searchKb("lease");
    expect(String(spy.mock.calls[0][0])).not.toContain("vault");
  });

  it("passes the vault id when the search is scoped to one", async () => {
    const spy = captureFetch(200, { results: [] });
    await api.searchKb("lease", "hybrid", 10, "v-123");
    expect(String(spy.mock.calls[0][0])).toContain("vault=v-123");
  });

  it("marks an export Desktop-local (x-sb-local), so the phone bridge cannot forward it", async () => {
    const spy = captureFetch(200, {});
    await api.exportVault("v-1", "pw");
    const init = spy.mock.calls[0][1]!;
    expect((init.headers as Record<string, string>)["x-sb-local"]).toBe("1");
    expect(init.method).toBe("POST");
  });

  it("surfaces the server's detail when an export is refused", async () => {
    captureFetch(403, { detail: "desktop only" });
    await expect(api.exportVault("v-1", "pw")).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
      message: "desktop only",
    });
  });

  it("sends the vault key in the query and the file as the raw body on import", async () => {
    const spy = captureFetch(200, { id: "v-2", name: "Shared", publisher: "SB-AAAA" });
    const file = new File(["sealed-bytes"], "expert.sbvault");
    await api.importVault(file, "SBVK1-abc");
    expect(String(spy.mock.calls[0][0])).toContain("key=SBVK1-abc");
    expect(spy.mock.calls[0][1]!.body).toBe(file);
  });
});
