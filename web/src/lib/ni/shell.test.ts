import { describe, expect, it } from "vitest";
import { bodyShowsPanel, cardSpan, cardState, footerText, formBoxChrome } from "./shell";
import type { NiBoardItem } from "$lib/api";

// A valid board row with every field a real /board response always sends; each test
// overrides only what it cares about (NiBoardItem has no optional required field).
function item(overrides: Partial<NiBoardItem>): NiBoardItem {
  return {
    id: "it1",
    title: "NVDA",
    state: "live",
    enabled: true,
    interval_minutes: 5,
    last_checked: "2026-10-07T12:00:00Z",
    last_status: "ok",
    consecutive_failures: 0,
    position: 0,
    display: { size: "small" },
    payload: { type: "divider" },
    payload_slot: "latest",
    payload_at: "2026-10-07T12:00:00Z",
    interpreted: false,
    ...overrides,
  };
}

describe("cardState", () => {
  it("reads a draft as designing", () => {
    expect(cardState(item({ state: "draft" }))).toBe("designing");
  });

  it("reads any item with an active flow as designing, even mid-commission", () => {
    expect(cardState(item({ state: "commissioning", flow: { state: "sampling" } })))
      .toBe("designing");
  });

  it("reads a held awaiting_yes item with its reading as verifying", () => {
    expect(cardState(item({
      state: "commissioning",
      awaiting_yes: { from: "page", host: "example.com", title: "" },
    }))).toBe("verifying");
  });

  it("reads the plain C2 'is this right' item (payload, not a preview slot) as verifying", () => {
    expect(cardState(item({ state: "commissioning", payload_slot: "latest" })))
      .toBe("verifying");
  });

  it("reads a commissioning item with no payload yet as empty", () => {
    expect(cardState(item({
      state: "commissioning", payload: null, payload_slot: null, payload_at: null,
    }))).toBe("empty");
  });

  it("reads any settled item with no payload as empty", () => {
    expect(cardState(item({
      state: "live", payload: null, payload_slot: null, payload_at: null,
    }))).toBe("empty");
  });

  it("reads failing and degraded the same way", () => {
    expect(cardState(item({ state: "failing" }))).toBe("failing");
    expect(cardState(item({ state: "degraded" }))).toBe("failing");
  });

  it("reads broken as its own bucket", () => {
    expect(cardState(item({ state: "broken" }))).toBe("broken");
  });

  it("reads a fresh live item as fresh, a stale one as stale", () => {
    // isStale (cardState's own dependency) compares against the real wall clock when
    // cardState doesn't pass a `now` override, so these use offsets from Date.now()
    // rather than a fixed timestamp (never flaky relative to when the suite runs).
    const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();
    expect(cardState(item({ state: "live", interval_minutes: 5, payload_at: minutesAgo(2) })))
      .toBe("fresh");
    expect(cardState(item({ state: "live", interval_minutes: 5, payload_at: minutesAgo(60) })))
      .toBe("stale");
  });

  it("reads the user's Pause toggle as paused ahead of its underlying health", () => {
    expect(cardState(item({ state: "failing", enabled: false }))).toBe("paused");
  });

  it("reads the (unused in production) NiState paused the same way", () => {
    expect(cardState(item({ state: "paused" }))).toBe("paused");
  });
});

describe("footerText", () => {
  it("joins as-of time and cadence when there is no host to show", () => {
    const text = footerText(item({ payload_at: "2026-10-07T12:00:00Z", interval_minutes: 30 }));
    expect(text.endsWith("every 30m")).toBe(true);
    expect(text.startsWith("as of ")).toBe(true);
    expect(text).not.toContain("··");
  });

  it("inserts the host for a held item that carries one", () => {
    const text = footerText(item({
      payload_at: "2026-10-07T12:00:00Z",
      interval_minutes: 10,
      awaiting_yes: { from: "dataset", host: "api.example.com", title: "" },
    }));
    // The as-of clock text is locale/timezone-dependent (Intl, no override) — only the
    // host + cadence segments, which this function fully controls, are asserted exactly.
    expect(text.startsWith("as of ")).toBe(true);
    expect(text.endsWith(" · api.example.com · every 10m")).toBe(true);
  });

  it("never drops the as-of segment, even with no payload yet", () => {
    expect(footerText(item({ payload_at: null }))).toBe("as of — · every 5m");
  });

  it("clamps a non-positive cadence to 1m (defensive; the server already clamps)", () => {
    expect(footerText(item({ payload_at: null, interval_minutes: 0 }))).toBe("as of — · every 1m");
  });
});

describe("cardSpan", () => {
  it("maps display.size when no CLIR is painted (legacy scene, absent, unknown)", () => {
    expect(cardSpan("small", null)).toEqual({ cols: 1, rows: 1 });
    expect(cardSpan(undefined, null)).toEqual({ cols: 1, rows: 1 });
    expect(cardSpan("wide", null)).toEqual({ cols: 2, rows: 1 });
    expect(cardSpan("large", null)).toEqual({ cols: 2, rows: 2 });
    expect(cardSpan("tall", null)).toEqual({ cols: 1, rows: 2 });
    expect(cardSpan("huge" as never, undefined)).toEqual({ cols: 1, rows: 1 });
  });

  it("takes the painted CLIR's own span key over display.size", () => {
    // engine pairs seen 2026-10-07: a 1x1 desktop stat seals a full-width 2-row phone face
    expect(cardSpan("small", { span: "p2x2" })).toEqual({ cols: 2, rows: 2 });
    // a 1x2 desktop design the engine still labels "large" is 1 column x 2 rows
    expect(cardSpan("large", { span: "d1x2" })).toEqual({ cols: 1, rows: 2 });
    // a 3-row phone face (tides event_curve)
    expect(cardSpan("large", { span: "p2x3" })).toEqual({ cols: 2, rows: 3 });
    expect(cardSpan("small", { span: "d1x1" })).toEqual({ cols: 1, rows: 1 });
    expect(cardSpan("wide", { span: "d2x1" })).toEqual({ cols: 2, rows: 1 });
  });

  it("clamps columns at 2 and falls back on an unparseable key", () => {
    expect(cardSpan("large", { span: "d4x1" })).toEqual({ cols: 2, rows: 1 });
    expect(cardSpan("wide", { span: "nope" })).toEqual({ cols: 2, rows: 1 });
    expect(cardSpan("wide", { span: 7 })).toEqual({ cols: 2, rows: 1 });
    expect(cardSpan("wide", {})).toEqual({ cols: 2, rows: 1 });
  });
});

describe("bodyShowsPanel", () => {
  it("is true exactly when VerifyPanel will show: commissioning, a non-preview form payload, not yet confirmed", () => {
    expect(bodyShowsPanel(item({ state: "commissioning", payload_slot: "latest" }), true))
      .toBe(true);
  });

  it("is false for a non-form payload — NiScene keeps painting, there is no panel to defer to", () => {
    expect(bodyShowsPanel(item({ state: "commissioning", payload_slot: "latest" }), false))
      .toBe(false);
  });

  it("is false once c2_ok (the confirmed-verifying message shows, not the panel)", () => {
    expect(bodyShowsPanel(item({ state: "commissioning", payload_slot: "latest", c2_ok: true }), true))
      .toBe(false);
  });

  it("is false for the held-source awaiting_yes screen, even with a form payload (ruled: unchanged)", () => {
    expect(bodyShowsPanel(item({
      state: "commissioning",
      awaiting_yes: { from: "page", host: "example.com", title: "" },
    }), true)).toBe(false);
  });

  it("is false while a flow is active, and false for any settled (non-commissioning) state", () => {
    expect(bodyShowsPanel(item({ state: "commissioning", flow: { state: "sampling" } }), true))
      .toBe(false);
    expect(bodyShowsPanel(item({ state: "live" }), true)).toBe(false);
  });

  it("is false with no payload yet (nothing for the panel to preview either)", () => {
    expect(bodyShowsPanel(item({
      state: "commissioning", payload: null, payload_slot: null, payload_at: null,
    }), true)).toBe(false);
  });
});

describe("formBoxChrome", () => {
  it("is true for a healthy form card: fresh, stale or paused", () => {
    expect(formBoxChrome("fresh", true)).toBe(true);
    expect(formBoxChrome("stale", true)).toBe(true);
    expect(formBoxChrome("paused", true)).toBe(true);
  });

  it("is false for the same three states on a legacy (non-form) card", () => {
    expect(formBoxChrome("fresh", false)).toBe(false);
    expect(formBoxChrome("stale", false)).toBe(false);
    expect(formBoxChrome("paused", false)).toBe(false);
  });

  it("is false for verifying and designing — their body is never a bare CLIR plane", () => {
    expect(formBoxChrome("verifying", true)).toBe(false);
    expect(formBoxChrome("designing", true)).toBe(false);
  });

  it("is false for failing/broken/empty — they may carry a reason line or Fix in `below`", () => {
    expect(formBoxChrome("failing", true)).toBe(false);
    expect(formBoxChrome("broken", true)).toBe(false);
    expect(formBoxChrome("empty", true)).toBe(false);
  });
});
