// The page-browser helpers behind the NI board (Round 20, B3b; ni-format §35). Rows and flow
// fields are built the way the server sends them (ni_routes._render_fields, ni_flow.tap_notice /
// the `browser` progress field), and every helper must read an older server's row — the fields
// absent — as "nothing to show".

import { describe, expect, it } from "vitest";

import { ApiError, type NiBoardItem, type NiItemFlow } from "$lib/api";
import {
  alsoLoadsLine,
  browserWaitLabel,
  capacityNote,
  confirmedNote,
  errorCapacity,
  feedCandidates,
  RENDER_BUSY,
  RENDER_UNAVAILABLE,
  renderDraftSites,
  renderNotice,
  replyCapacity,
  tapClause,
} from "./browser";

function item(overrides: Partial<NiBoardItem>): NiBoardItem {
  return {
    id: "it1", title: "Gas prices", state: "live", enabled: true, interval_minutes: 15,
    last_checked: "2026-10-12T10:00:00Z", last_status: "ok", consecutive_failures: 0, position: 0,
    display: { size: "small" }, payload: null, payload_slot: null, payload_at: null, interpreted: false,
    template_update: false, template_gone: false, needs_credentials: [], needs_params: [], l2_proposal: false,
    c2_ok: false, awaiting_yes: null, shell: false, born: "", flow: null, payload_ok: null, actions: [],
    ...overrides,
  };
}

const SAMPLING: NiItemFlow = { state: "sampling" };

describe("browserWaitLabel — the build waits for the page browser", () => {
  it("names each getting-ready phase with its percentage", () => {
    for (const phase of ["downloading", "verifying", "unpacking", "self-testing"]) {
      expect(browserWaitLabel({ ...SAMPLING, browser: { phase, pct: 43 } })).toBe("Preparing the page browser (43%)…");
    }
  });

  it("says nothing for a settled phase, a missing field or no flow", () => {
    for (const phase of ["absent", "ready", "error", "unavailable", "stale", "disabled", "nonsense"]) {
      expect(browserWaitLabel({ ...SAMPLING, browser: { phase, pct: 100 } })).toBe("");
    }
    expect(browserWaitLabel(SAMPLING)).toBe("");
    expect(browserWaitLabel(null)).toBe("");
    expect(browserWaitLabel(undefined)).toBe("");
  });

  it("rounds and clamps the percentage", () => {
    expect(browserWaitLabel({ ...SAMPLING, browser: { phase: "downloading", pct: 42.6 } })).toBe("Preparing the page browser (43%)…");
    expect(browserWaitLabel({ ...SAMPLING, browser: { phase: "downloading", pct: 140 } })).toBe("Preparing the page browser (100%)…");
    expect(browserWaitLabel({ ...SAMPLING, browser: { phase: "downloading", pct: -3 } })).toBe("Preparing the page browser (0%)…");
    expect(browserWaitLabel({ ...SAMPLING, browser: { phase: "downloading", pct: Number.NaN } })).toBe("Preparing the page browser (0%)…");
  });
});

describe("tapClause — what the paste's consent covers", () => {
  const PICK: NiItemFlow = { state: "source", error: "awaiting_pick", suggestions: [] };

  it("adds the closed clause with the site cap when a pasted page may render here", () => {
    expect(tapClause({ ...PICK, tap: { render_notice: true, site_cap: 12 } })).toBe(
      "If the page needs SmartBrain's page browser, that one visit may also load the sites it depends on (up to 12).",
    );
  });

  it("adds nothing when the page browser can't run here, or the server sent no notice", () => {
    expect(tapClause({ ...PICK, tap: { render_notice: false, site_cap: 12 } })).toBe("");
    expect(tapClause(PICK)).toBe("");
    expect(tapClause(null)).toBe("");
  });

  it("drops a cap that is not a positive whole number rather than print it", () => {
    const clause = tapClause({ ...PICK, tap: { render_notice: true, site_cap: 0 } });
    expect(clause).toBe("If the page needs SmartBrain's page browser, that one visit may also load the sites it depends on.");
    expect(tapClause({ ...PICK, tap: { render_notice: true, site_cap: 2.5 } })).toBe(clause);
  });
});

describe("renderDraftSites — the sites a draft waits to be allowed", () => {
  it("lists the other sites in the sealed order, without the page's own (first)", () => {
    const row = item({ state: "draft", draft: { reason: "render_sites", sites: ["gasbuddy.com", "cloudfront.net", "gstatic.com"] } });
    expect(renderDraftSites(row)).toEqual(["cloudfront.net", "gstatic.com"]);
  });

  it("is null for a row that is no such draft — the plain Activate shows", () => {
    expect(renderDraftSites(item({ state: "draft" }))).toBeNull();
    expect(renderDraftSites(item({ state: "draft", draft: null }))).toBeNull();
    expect(renderDraftSites(item({ state: "draft", draft: { reason: "other" as "render_sites", sites: ["a.com", "b.com"] } }))).toBeNull();
    expect(renderDraftSites(item({ state: "draft", draft: { reason: "render_sites", sites: ["a.com"] } }))).toBeNull();
  });

  it("drops repeats and blanks and keeps at most 12", () => {
    const many = ["own.com", ...Array.from({ length: 14 }, (_, i) => `s${i}.com`)];
    expect(renderDraftSites(item({ draft: { reason: "render_sites", sites: many } }))).toHaveLength(12);
    const messy = ["own.com", "a.com", "a.com", " ", "b.com"];
    expect(renderDraftSites(item({ draft: { reason: "render_sites", sites: messy } }))).toEqual(["a.com", "b.com"]);
  });

  it("reads the count line in plain words", () => {
    expect(alsoLoadsLine(1)).toBe("This page also loads 1 other site");
    expect(alsoLoadsLine(3)).toBe("This page also loads 3 other sites");
  });
});

describe("renderNotice — the page browser unavailable here", () => {
  it("is the state line for a rendered card whose last update found no page browser", () => {
    const row = item({ rendered: true, render: { sites: ["gasbuddy.com"], unavailable: true } });
    expect(renderNotice(row)).toBe(RENDER_UNAVAILABLE);
    expect(RENDER_UNAVAILABLE).toContain("unavailable on this machine");
    expect(RENDER_UNAVAILABLE).toContain("isn't a failure");
  });

  it("is empty for a rendered card that updates, and for every other card", () => {
    expect(renderNotice(item({ rendered: true, render: { sites: ["a.com"], unavailable: false } }))).toBe("");
    expect(renderNotice(item({}))).toBe("");
    expect(renderNotice(item({ render: { sites: ["a.com"], unavailable: true } }))).toBe("");
  });
});

describe("feedCandidates — the site's own data feeds", () => {
  it("keeps the exact https addresses in the server's order", () => {
    const feeds = [
      { url: "https://api.gasbuddy.com/v2/prices?zip=27514&fuel=1", site: "gasbuddy.com" },
      { url: "https://gasbuddy.com/graphql.json", site: "gasbuddy.com" },
    ];
    expect(feedCandidates(item({ preview: { feed_candidates: feeds } }))).toEqual(feeds);
  });

  it("drops non-https and repeated addresses, and keeps at most 5", () => {
    const raw = [
      { url: "http://a.com/x.json", site: "a.com" },
      { url: "https://a.com/x.json", site: "a.com" },
      { url: "https://a.com/x.json", site: "a.com" },
      { url: "javascript:alert(1)", site: "a.com" },
    ];
    expect(feedCandidates(item({ preview: { feed_candidates: raw } }))).toEqual([{ url: "https://a.com/x.json", site: "a.com" }]);
    const seven = Array.from({ length: 7 }, (_, i) => ({ url: `https://a.com/${i}.json`, site: "a.com" }));
    expect(feedCandidates(item({ preview: { feed_candidates: seven } }))).toHaveLength(5);
  });

  it("is empty when the row carries none (absent, null, or an older server)", () => {
    expect(feedCandidates(item({}))).toEqual([]);
    expect(feedCandidates(item({ preview: null }))).toEqual([]);
    // an older server's null list, which the current wire never sends
    expect(feedCandidates(item({ preview: { feed_candidates: null } } as unknown as Partial<NiBoardItem>))).toEqual([]);
  });
});

const BUSY_SENTENCE = "the local page browser is busy — try again in a moment";

describe("errorCapacity — a refused request's page-browser answer", () => {
  it("reads the server's code first: render_busy and render_unavailable", () => {
    expect(errorCapacity(new ApiError(409, BUSY_SENTENCE, "render_busy"))).toBe("busy");
    expect(errorCapacity(new ApiError(409, "the page browser is unavailable here", "render_unavailable"))).toBe("unavailable");
    // the code decides even when the sentence or the status says something else
    expect(errorCapacity(new ApiError(409, "reworded by a newer server", "render_busy"))).toBe("busy");
    expect(errorCapacity(new ApiError(503, BUSY_SENTENCE, "render_unavailable"))).toBe("unavailable");
  });

  it("trusts any other code over the sentence — it is another refusal", () => {
    expect(errorCapacity(new ApiError(409, BUSY_SENTENCE, "item_paused"))).toBe("");
    expect(errorCapacity(new ApiError(409, BUSY_SENTENCE, "constructor"))).toBe("");
  });

  it("falls back to the busy 409's sentence from a server that sends no code", () => {
    expect(errorCapacity(new ApiError(409, BUSY_SENTENCE))).toBe("busy");
    expect(errorCapacity(new ApiError(409, BUSY_SENTENCE, ""))).toBe("busy");
  });

  it("is no capacity answer for every other refusal or failure", () => {
    expect(errorCapacity(new ApiError(409, "run refused: item paused"))).toBe("");
    expect(errorCapacity(new ApiError(500, "the local page browser is busy"))).toBe("");
    expect(errorCapacity(new Error("the local page browser is busy"))).toBe("");
    expect(errorCapacity("busy")).toBe("");
  });
});

describe("replyCapacity — a run's or a C2 verdict's page-browser answer", () => {
  it("reads the server's code first", () => {
    expect(replyCapacity({ code: "render_busy", run: "busy" })).toBe("busy");
    expect(replyCapacity({ code: "render_unavailable", run: "error" })).toBe("unavailable");
    expect(replyCapacity({ code: "render_unavailable", kind: "render_unavailable" })).toBe("unavailable");
    expect(replyCapacity({ code: "render_busy" })).toBe("busy");
  });

  it("trusts any other code over the older fields", () => {
    expect(replyCapacity({ code: "something_else", run: "busy" })).toBe("");
    expect(replyCapacity({ code: "toString", kind: "render_unavailable" })).toBe("");
  });

  it("falls back to `run: \"busy\"` (C2) and `kind: \"render_unavailable\"` (a run) without a code", () => {
    expect(replyCapacity({ run: "busy" })).toBe("busy");
    expect(replyCapacity({ kind: "render_unavailable" })).toBe("unavailable");
  });

  it("is no capacity answer for every other reply", () => {
    expect(replyCapacity({ run: "ok" })).toBe("");
    expect(replyCapacity({ run: "error" })).toBe("");
    expect(replyCapacity({ kind: "http_5xx" })).toBe("");
    expect(replyCapacity({})).toBe("");
    expect(replyCapacity(null)).toBe("");
    expect(replyCapacity(undefined)).toBe("");
  });
});

describe("capacityNote and confirmedNote — what the board says instead of an error", () => {
  it("has a run note for each capacity answer, none for no answer", () => {
    expect(capacityNote("busy")).toBe(RENDER_BUSY);
    expect(capacityNote("unavailable")).toBe(RENDER_UNAVAILABLE);
    expect(capacityNote("")).toBe("");
  });

  it("confirms a C2 YES and says when the card checks its data", () => {
    expect(confirmedNote("busy")).toBe("Confirmed — the page browser is busy, so the card checks its data on its next update.");
    expect(confirmedNote("unavailable")).toContain("unavailable on this machine");
    expect(confirmedNote("unavailable").startsWith("Confirmed — ")).toBe(true);
    expect(confirmedNote("")).toBe("");
  });
});
