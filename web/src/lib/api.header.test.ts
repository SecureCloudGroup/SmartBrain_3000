// The req<T> wrapper owns the Content-Type header. Live 2026-10-26: two callers spread their own
// "Content-Type" over the wrapper's "content-type"; a plain object keeps both keys, fetch merges
// them into "application/json, application/json", and FastAPI then refuses the body (422) — every
// card answer (Allow / Use local / a date) and every Refine failed with "That didn't go through".
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { api } = await import("./api");

let realFetch: typeof globalThis.fetch;
beforeEach(() => {
  realFetch = globalThis.fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

function capture(): { init: () => RequestInit | undefined } {
  let seen: RequestInit | undefined;
  globalThis.fetch = vi.fn(async (_url: unknown, init?: RequestInit) => {
    seen = init;
    return new Response(JSON.stringify({ ok: true, started: true }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as unknown as typeof globalThis.fetch;
  return { init: () => seen };
}

function contentTypes(init: RequestInit | undefined): string[] {
  const out: string[] = [];
  new Headers(init?.headers).forEach((v, k) => {
    if (k === "content-type") out.push(v);
  });
  return out;
}

describe("req wrapper sends exactly one Content-Type", () => {
  it("a card answer (the call that failed live) carries one application/json", async () => {
    const seen = capture();
    await api.niFlowAnswer("item-1", "model_consent", "allow");
    expect(contentTypes(seen.init())).toEqual(["application/json"]);
    expect(seen.init()?.body).toBe(JSON.stringify({ kind: "model_consent", value: "allow" }));
  });

  it("Refine, the other caller that doubled it, carries one too", async () => {
    const seen = capture();
    await api.niRefine("item-1", "every 10 minutes");
    expect(contentTypes(seen.init())).toEqual(["application/json"]);
  });

  it("a plain POST keeps the default", async () => {
    const seen = capture();
    await api.niIntake("show me AAPL every 5 minutes");
    expect(contentTypes(seen.init())).toEqual(["application/json"]);
  });
});
