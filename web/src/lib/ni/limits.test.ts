// The card's lines about its source's free request limit and a host's wait (ni_routes._limit_line, _waiting_of).
// Rows are built the way the server sends them, and an older server's row (both fields absent) shows nothing.

import { describe, expect, it } from "vitest";

import type { NiBoardItem } from "$lib/api";
import { keepLimitLine, tryAgainAt, waitingNotice } from "./limits";

function item(overrides: Partial<NiBoardItem>): NiBoardItem {
  return {
    id: "it1", title: "NVDA", state: "live", enabled: true, interval_minutes: 30,
    last_checked: "2026-10-31T10:00:00Z", last_status: "ok", consecutive_failures: 0, position: 0,
    display: { size: "small" }, payload: null, payload_slot: null, payload_at: null, interpreted: false,
    template_update: false, template_gone: false, needs_credentials: [], needs_params: [], l2_proposal: false,
    c2_ok: false, awaiting_yes: null, shell: false, born: "", flow: null, payload_ok: null, actions: [],
    ...overrides,
  };
}

const AV_LINE = "Alpha Vantage's free key allows 25 requests a day. Refreshing every 30 minutes needs 48, so this card "
  + "will stop updating partway through each day.";
const UNTIL = "2026-10-31T19:15:00+00:00";

describe("keepLimitLine — the line under Keep", () => {
  it("shows the backend's line as it is", () => {
    expect(keepLimitLine(item({ limit: AV_LINE }))).toBe(AV_LINE);
  });
  it("shows nothing when the limit covers the cadence or an older server sent no field", () => {
    expect(keepLimitLine(item({}))).toBe("");
  });
});

describe("tryAgainAt — the next try in the viewer's clock", () => {
  it("formats the hour and minute", () => {
    expect(tryAgainAt(UNTIL, "en-US")).toMatch(/^\d{1,2}:15\s?(AM|PM)$/);
  });
  it("says nothing for a time that doesn't parse", () => {
    expect(tryAgainAt("soon")).toBe("");
  });
});

describe("waitingNotice — the card while its host holds SmartBrain off", () => {
  it("names the known limit and the next try, not the host's long sentence", () => {
    const row = item({ limit: AV_LINE, waiting: { host: "www.alphavantage.co", until: UNTIL, said: "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day." } });
    const note = waitingNotice(row, "en-US");
    expect(note.startsWith(`${AV_LINE} www.alphavantage.co asked SmartBrain to wait. The card tries again at `)).toBe(true);
    expect(note).not.toContain("Thank you");
  });
  it("gives the host's own words when no limit is known", () => {
    const row = item({ waiting: { host: "api.example.org", until: UNTIL, said: "Too many requests, slow down." } });
    expect(waitingNotice(row, "en-US")).toMatch(
      /^api\.example\.org asked SmartBrain to wait: “Too many requests, slow down\.” The card tries again at \d{1,2}:15\s?(AM|PM)\.$/);
  });
  it("says it waits when the host gave no words, and later when the time doesn't parse", () => {
    expect(waitingNotice(item({ waiting: { host: "h.example", until: "x", said: "" } }))).toBe(
      "h.example asked SmartBrain to wait. The card tries again later.");
  });
  it("shows nothing without a wait, and nothing on a card still waiting for Keep", () => {
    expect(waitingNotice(item({ limit: AV_LINE }))).toBe("");
    const held = item({ waiting: { host: "h.example", until: UNTIL, said: "" },
      awaiting_yes: { from: "library", host: "h.example", title: "Quote" } });
    expect(waitingNotice(held)).toBe("");
  });
});
