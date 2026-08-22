// The req<T> wrapper in api.ts is the choke-point for every typed call. The contract:
// non-2xx -> throw ApiError(status, detail-from-server); 423 also triggers a /unlock
// navigation; 2xx -> resolve to parsed JSON. Tests exercise the wrapper via api.health()
// because req itself is module-internal.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// $app/navigation + $lib/remote/sw-bridge are stubbed in vitest-setup.ts. The setup
// installs goto as vi.fn(); we read it here to assert the 423 -> /unlock side-effect.
const { goto } = await import("$app/navigation");
const gotoSpy = goto as unknown as ReturnType<typeof vi.fn>;
const { api, ApiError, parseExportHeaders } = await import("./api");

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

  it("notifies the registered lock handler on 423 (breaks the stale-unlocked stampede)", async () => {
    const { registerLockedHandler } = await import("./api");
    const onLocked = vi.fn();
    registerLockedHandler(onLocked);
    globalThis.fetch = vi.fn(async () =>
      jsonResponse(423, { detail: "locked" }),
    ) as unknown as typeof globalThis.fetch;
    await expect(api.health()).rejects.toMatchObject({ status: 423 });
    expect(onLocked).toHaveBeenCalledTimes(1);
  });

  it("does NOT re-navigate when already on /unlock (no redirect loop)", async () => {
    // This suite runs DOM-less; stand in a minimal window to simulate the tab
    // already sitting on the unlock page.
    (globalThis as { window?: unknown }).window = { location: { pathname: "/unlock" } };
    try {
      globalThis.fetch = vi.fn(async () =>
        jsonResponse(423, { detail: "locked" }),
      ) as unknown as typeof globalThis.fetch;
      await expect(api.health()).rejects.toMatchObject({ status: 423 });
      expect(gotoSpy).not.toHaveBeenCalled();
    } finally {
      delete (globalThis as { window?: unknown }).window;
    }
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

  it("returns { blob, headers } so the UI can warn on unchanged/rotated-key without a second call", async () => {
    const spy = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
      new Response(new Blob(["hi"]), {
        status: 200,
        headers: {
          "x-sb-export-seq": "4",
          "x-sb-export-mode": "sealed",
          "x-sb-export-rotated-key": "1",
        },
      }),
    );
    globalThis.fetch = spy as unknown as typeof globalThis.fetch;
    const { blob, headers } = await api.exportVault("v-1", "pw");
    expect(blob).toBeInstanceOf(Blob);
    expect(headers.seq).toBe(4);
    expect(headers.mode).toBe("sealed");
    expect(headers.rotatedKey).toBe(true);
    expect(headers.unchanged).toBe(false);
    expect(headers.retired).toBe(false);
  });

  it("surfaces the server's detail when an export is refused", async () => {
    captureFetch(403, { detail: "desktop only" });
    await expect(api.exportVault("v-1", "pw")).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
      message: "desktop only",
    });
  });

  it("retireVault posts to /retire with the passphrase body and the Desktop-local marker", async () => {
    const spy = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
      new Response(new Blob(["final"]), {
        status: 200,
        headers: {
          "x-sb-export-seq": "9",
          "x-sb-export-mode": "open",
          "x-sb-export-retired": "1",
        },
      }),
    );
    globalThis.fetch = spy as unknown as typeof globalThis.fetch;
    const { headers } = await api.retireVault("v-1", "pw");
    expect(String(spy.mock.calls[0][0])).toBe("/api/vaults/v-1/retire");
    const init = spy.mock.calls[0][1]!;
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["x-sb-local"]).toBe("1");
    expect(JSON.parse(String(init.body))).toEqual({ passphrase: "pw" });
    expect(headers.retired).toBe(true);
    expect(headers.mode).toBe("open");
  });

  it("deleteVault(id) keeps documents by default — no query string", async () => {
    const spy = captureFetch(200, { ok: true, removed_docs: 0 });
    await api.deleteVault("v-1");
    expect(String(spy.mock.calls[0][0])).toBe("/api/vaults/v-1");
    expect(spy.mock.calls[0][1]!.method).toBe("DELETE");
  });

  it("deleteVault(id, {remove_docs:true}) adds ?remove_docs=1 and returns the count", async () => {
    const spy = captureFetch(200, { ok: true, removed_docs: 5 });
    const r = await api.deleteVault("v-1", { remove_docs: true });
    expect(String(spy.mock.calls[0][0])).toBe("/api/vaults/v-1?remove_docs=1");
    expect(r.removed_docs).toBe(5);
  });

  it("updateVaultMeta carries hosted_url in the PATCH body when the field is provided", async () => {
    // The hosted-URL note is publisher-local metadata: the UI Save button writes it via the same
    // PATCH the rename/tags editors use. Absent = untouched (a rename must not wipe it); an empty
    // string clears it — mirrors the tags rule and matches the backend semantics.
    const spy = captureFetch(200, { id: "v-1" });
    await api.updateVaultMeta("v-1", { name: "Expert", hosted_url: "https://ex.com/e.sbvault" });
    const [url, init] = spy.mock.calls[0];
    expect(String(url)).toBe("/api/vaults/v-1");
    expect(init!.method).toBe("PATCH");
    expect(JSON.parse(String(init!.body))).toEqual({
      name: "Expert", hosted_url: "https://ex.com/e.sbvault",
    });
  });

  it("updateVaultMeta with hosted_url:'' sends the empty string so the server clears it", async () => {
    // "" is a distinct value the server reads as "clear the note"; if the client dropped it, the
    // Save-with-empty flow would silently no-op and the user would never be able to un-set the URL.
    const spy = captureFetch(200, { id: "v-1" });
    await api.updateVaultMeta("v-1", { name: "Expert", hosted_url: "" });
    expect(JSON.parse(String(spy.mock.calls[0][1]!.body))).toEqual({
      name: "Expert", hosted_url: "",
    });
  });

  it("verifyHostedVault posts to /verify-hosted with the Desktop-local marker header", async () => {
    // verify-hosted is Desktop-local (its verdict names this install's own publisher key). A
    // dropped x-sb-local header is a 403 through the bridge — pin the header here so a phone
    // regression turns into a red test rather than a broken UI on the phone.
    const spy = captureFetch(200, {
      reachable: true, seq: 4, matches: true, behind: false, retired: false,
      detail: "the hosted file matches what this install last published (v4)",
    });
    const r = await api.verifyHostedVault("v-1");
    const [url, init] = spy.mock.calls[0];
    expect(String(url)).toBe("/api/vaults/v-1/verify-hosted");
    expect(init!.method).toBe("POST");
    expect((init!.headers as Record<string, string>)["x-sb-local"]).toBe("1");
    expect(r).toEqual({
      reachable: true, seq: 4, matches: true, behind: false, retired: false,
      detail: "the hosted file matches what this install last published (v4)",
    });
  });

  it("verifyHostedVault surfaces the server's detail on a 400 (no hosted URL set)", async () => {
    // The endpoint's 400 detail is human-ready — the UI renders it inline. A silent generic fallback
    // would strip the "add a URL first" hint the server carefully wrote.
    captureFetch(400, { detail: "this vault has no hosted URL set — add one first" });
    await expect(api.verifyHostedVault("v-1")).rejects.toMatchObject({
      name: "ApiError",
      status: 400,
      message: "this vault has no hosted URL set — add one first",
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

// parseExportHeaders is exported so a test can PIN the exact header names the UI reads. A
// silent server-side rename ("x-sb-export-rotated" → "x-sb-export-rekey") would otherwise
// simply stop firing the sealed re-key warning, with nothing catching the drift.
describe("parseExportHeaders — the /export + /retire response header shape", () => {
  it("returns typed defaults when none of the x-sb-export-* headers are present", () => {
    const parsed = parseExportHeaders(new Headers({}));
    expect(parsed).toEqual({
      seq: null, mode: null, unchanged: false, rotatedKey: false, retired: false,
    });
  });

  it("reads seq as an integer and rejects a non-numeric one", () => {
    expect(parseExportHeaders(new Headers({ "x-sb-export-seq": "12" })).seq).toBe(12);
    expect(parseExportHeaders(new Headers({ "x-sb-export-seq": "abc" })).seq).toBeNull();
  });

  it("accepts sealed|open for mode; anything else is null (no UI would render it)", () => {
    expect(parseExportHeaders(new Headers({ "x-sb-export-mode": "sealed" })).mode).toBe("sealed");
    expect(parseExportHeaders(new Headers({ "x-sb-export-mode": "open" })).mode).toBe("open");
    expect(parseExportHeaders(new Headers({ "x-sb-export-mode": "weird" })).mode).toBeNull();
  });

  it("maps the three '1'-valued flags to booleans", () => {
    const parsed = parseExportHeaders(new Headers({
      "x-sb-export-unchanged": "1",
      "x-sb-export-rotated-key": "1",
      "x-sb-export-retired": "1",
    }));
    expect(parsed.unchanged).toBe(true);
    expect(parsed.rotatedKey).toBe(true);
    expect(parsed.retired).toBe(true);
  });
});

// Approval + remembered-consent client. The Activity page reads listRemembered as
// { tools, sites }, and its "Always allow www.zerohedge.com" button flows through
// approveAction with `remember: true`. Stop-allowing a site row is a DELETE that must
// carry ?host= — without it the server would drop the WHOLE-tool consent instead.
describe("approvals + remembered consent", () => {
  function captureFetch(status = 200, body: unknown = {}) {
    const spy = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse(status, body),
    );
    globalThis.fetch = spy as unknown as typeof globalThis.fetch;
    return spy;
  }

  it("approve sends {confirm_tool, remember} in the JSON body", async () => {
    const spy = captureFetch(200, { status: "executed", result: {} });
    await api.approveAction("pid-1", null, true);
    const [url, init] = spy.mock.calls[0];
    expect(String(url)).toBe("/api/agent/pending/pid-1/approve");
    expect(init!.method).toBe("POST");
    expect(JSON.parse(String(init!.body))).toEqual({ confirm_tool: null, remember: true });
  });

  it("listRemembered returns the { tools, sites } shape (site rows carry tool + host)", async () => {
    captureFetch(200, {
      tools: ["kb_add"],
      sites: [{ tool: "web_fetch", host: "www.zerohedge.com" }],
    });
    const r = await api.listRemembered();
    expect(r.tools).toEqual(["kb_add"]);
    expect(r.sites).toEqual([{ tool: "web_fetch", host: "www.zerohedge.com" }]);
  });

  it("forgetRemembered without a host drops WHOLE-tool consent (no query string)", async () => {
    const spy = captureFetch(200, { ok: true });
    await api.forgetRemembered("kb_add");
    const [url, init] = spy.mock.calls[0];
    expect(String(url)).toBe("/api/agent/remembered/kb_add");
    expect(init!.method).toBe("DELETE");
    expect(String(url)).not.toContain("host=");
  });

  it("forgetRemembered with a host encodes it as ?host= so only that site row is dropped", async () => {
    const spy = captureFetch(200, { ok: true });
    await api.forgetRemembered("web_fetch", "www.zerohedge.com");
    const [url, init] = spy.mock.calls[0];
    expect(String(url)).toBe("/api/agent/remembered/web_fetch?host=www.zerohedge.com");
    expect(init!.method).toBe("DELETE");
  });
});

// Self-improvement cadence: the Settings segmented control sends ONE PUT per click.
// Picking an hour flips the reviewer on AND sets the cadence together (a two-request
// dance would leave a valid state visible mid-flight — off-with-new-cadence — and give
// the server an extra chance to fail between them). Picking Off must NOT touch the
// stored interval, so the body carries only enabled:false — the server preserves it.
describe("self-improve cadence", () => {
  function captureFetch(status = 200, body: unknown = {}) {
    const spy = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse(status, body),
    );
    globalThis.fetch = spy as unknown as typeof globalThis.fetch;
    return spy;
  }

  it("putSelfImprove(off) sends only { enabled: false } in one PUT", async () => {
    const spy = captureFetch(200, { enabled: false, interval_hours: 8, last_run: null });
    await api.putSelfImprove({ enabled: false });
    expect(spy.mock.calls).toHaveLength(1);
    const [url, init] = spy.mock.calls[0];
    expect(String(url)).toBe("/api/selfimprove");
    expect(init!.method).toBe("PUT");
    expect(JSON.parse(String(init!.body))).toEqual({ enabled: false });
  });

  it("putSelfImprove(interval) sends both enabled:true and the interval_hours in one PUT", async () => {
    const spy = captureFetch(200, { enabled: true, interval_hours: 4, last_run: null });
    await api.putSelfImprove({ enabled: true, interval_hours: 4 });
    expect(spy.mock.calls).toHaveLength(1);
    const [url, init] = spy.mock.calls[0];
    expect(String(url)).toBe("/api/selfimprove");
    expect(init!.method).toBe("PUT");
    expect(JSON.parse(String(init!.body))).toEqual({ enabled: true, interval_hours: 4 });
  });

  it("getSelfImprove returns { enabled, interval_hours, last_run }", async () => {
    captureFetch(200, { enabled: true, interval_hours: 2, last_run: "2026-08-01 12:00:00.000000" });
    const state = await api.getSelfImprove();
    expect(state.enabled).toBe(true);
    expect(state.interval_hours).toBe(2);
    expect(state.last_run).toBe("2026-08-01 12:00:00.000000");
  });
});
