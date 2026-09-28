// Pure logic for the SmartBrain Library page (/ni/library). The page has no way
// to reach these branches except through these functions — so they pin the
// contract the page depends on (enum → plain words, query building, form check).

import { describe, expect, it } from "vitest";
import type {
  LibrarySourceRow,
  LibraryTaxonomyCat,
  LocalSourceInput,
} from "$lib/api";
import {
  accessKindLabel,
  accessKindOptions,
  authorityLabel,
  buildSearchQuery,
  formatCount,
  labelCategory,
  rowCategoryPhrase,
  ROW_CATEGORY_LIMIT,
  cadenceLabel,
  checkedOnLabel,
  statusChipKind,
  statusLabel,
  termsLabel,
  tierLabel,
  validateLocalForm,
} from "./libraryPage";

const TAX: LibraryTaxonomyCat[] = [
  {
    id: "weather",
    label: "Weather",
    count: 12,
    subcategories: [
      { id: "current", label: "Current conditions", count: 6 },
      { id: "tides", label: "Tides", count: 3 },
    ],
  },
  {
    id: "finance",
    label: "Finance",
    count: 8,
    subcategories: [{ id: "quote", label: "Stock quotes", count: 4 }],
  },
];

function mkRow(overrides: Partial<LibrarySourceRow> = {}): LibrarySourceRow {
  return {
    id: "s1",
    name: "NOAA Tides",
    description: "Water levels at US stations",
    provider: "NOAA",
    authority: "official",
    tier: "curated",
    geo: "us",
    access_kind: "http_json",
    auth: "none",
    terms: "public_domain",
    cadence: "6m",
    status: "ok",
    categories: ["weather/tides"],
    ...overrides,
  };
}

describe("statusLabel / statusChipKind", () => {
  it("maps ok/degraded/failed to plain words", () => {
    expect(statusLabel("ok", "none")).toBe("Works");
    expect(statusLabel("degraded", "none")).toBe("Degraded");
    expect(statusLabel("failed", "none")).toBe("Not working");
    expect(statusLabel("refused", "none")).toBe("Not working");
  });

  it("reframes unvalidated as 'Needs a key' when auth != none", () => {
    // The user sees WHAT to expect (an API key), not the internal validation state.
    expect(statusLabel("unvalidated", "free_key")).toBe("Needs a key");
    expect(statusLabel("unvalidated", "none")).toBe("Unchecked");
  });

  it("returns a Chip kind that never says 'no' when the row is fine", () => {
    expect(statusChipKind("ok", "none")).toBe("ok");
    expect(statusChipKind("degraded", "none")).toBe("warn");
    expect(statusChipKind("failed", "none")).toBe("danger");
    expect(statusChipKind("unvalidated", "free_key")).toBe(""); // neutral until we know
  });
});

describe("authorityLabel / tierLabel / accessKindLabel / termsLabel", () => {
  it("returns human words, never raw enum names", () => {
    expect(authorityLabel("official")).toBe("Official");
    expect(tierLabel("curated")).toBe("Curated");
    expect(tierLabel("local")).toBe("Yours");
    expect(accessKindLabel("http_json")).toBe("JSON");
    expect(accessKindLabel("gtfs_rt")).toBe("Transit realtime (GTFS-RT)");
    expect(termsLabel("public_domain")).toBe("Public domain");
    expect(termsLabel("unverified")).toBe("Terms not confirmed");
  });

  it("accessKindOptions covers every kind and never emits a raw id as label", () => {
    const opts = accessKindOptions();
    expect(opts.length).toBeGreaterThan(0);
    for (const o of opts) expect(o.label).not.toMatch(/_/); // no underscore-shaped ids leaking through
  });
});

describe("labelCategory / rowCategoryPhrase", () => {
  it("resolves 'weather/tides' to 'Weather › Tides'", () => {
    expect(labelCategory("weather/tides", TAX)).toBe("Weather › Tides");
  });

  it("resolves a top-level id to just the category label", () => {
    expect(labelCategory("finance", TAX)).toBe("Finance");
  });

  it("falls back to the raw id when the taxonomy doesn't declare it", () => {
    expect(labelCategory("unknown/x", TAX)).toBe("unknown/x");
  });

  it("shows at most two placements on a row", () => {
    const row = mkRow({ categories: ["weather/tides", "finance/quote", "weather/tides"] });
    expect(rowCategoryPhrase(row, TAX).split(" · ")).toHaveLength(ROW_CATEGORY_LIMIT);
  });

  it("joins multiple row categories with a middle dot", () => {
    const row = mkRow({ categories: ["weather/tides", "finance/quote"] });
    expect(rowCategoryPhrase(row, TAX)).toBe("Weather › Tides · Finance › Stock quotes");
  });

  it("returns empty string when a row has no categories", () => {
    expect(rowCategoryPhrase(mkRow({ categories: [] }), TAX)).toBe("");
  });
});

describe("buildSearchQuery", () => {
  it("omits every empty filter (never sends q=&category=&…)", () => {
    const out = buildSearchQuery({ q: "", category: "", subcategory: "", tier: "", offset: 0, limit: 20 });
    expect(out).toEqual({ offset: 0, limit: 20 });
  });

  it("trims the query string and forwards every set filter", () => {
    const out = buildSearchQuery({
      q: "  tides  ",
      category: "weather",
      subcategory: "tides",
      tier: "curated",
      offset: 40,
      limit: 20,
    });
    expect(out).toEqual({
      offset: 40,
      limit: 20,
      q: "tides",
      category: "weather",
      subcategory: "tides",
      tier: "curated",
    });
  });
});

describe("formatCount", () => {
  it("formats with US thousands separators", () => {
    expect(formatCount(9045)).toBe("9,045");
    expect(formatCount(0)).toBe("0");
  });

  it("falls back to '0' for missing / non-finite / negative inputs", () => {
    expect(formatCount(undefined)).toBe("0");
    expect(formatCount(null)).toBe("0");
    expect(formatCount(-3)).toBe("0");
    expect(formatCount(Number.NaN)).toBe("0");
  });
});

describe("validateLocalForm", () => {
  const ok: LocalSourceInput = {
    name: "My data",
    url: "https://example.com/data",
    description: "",
    category: "weather/tides",
    access_kind: "http_json",
    needs_key: false,
  };

  it("passes a well-formed input", () => {
    expect(validateLocalForm(ok)).toBeNull();
  });

  it("rejects a too-short name", () => {
    const err = validateLocalForm({ ...ok, name: "x" });
    expect(err?.field).toBe("name");
  });

  it("rejects a name over 120 characters", () => {
    const err = validateLocalForm({ ...ok, name: "n".repeat(121) });
    expect(err?.field).toBe("name");
  });

  it("rejects a non-https url", () => {
    const err = validateLocalForm({ ...ok, url: "http://example.com/x" });
    expect(err?.field).toBe("url");
    expect(err?.message).toMatch(/https/);
  });

  it("rejects a description over 600 characters", () => {
    const err = validateLocalForm({ ...ok, description: "x".repeat(601) });
    expect(err?.field).toBe("description");
  });

  it("rejects an empty or malformed category (must be 'cat/sub')", () => {
    expect(validateLocalForm({ ...ok, category: "" })?.field).toBe("category");
    expect(validateLocalForm({ ...ok, category: "weather" })?.field).toBe("category");
  });
});

describe("cadenceLabel / checkedOnLabel", () => {
  it("turns cadence ids into plain words", () => {
    expect(cadenceLabel("minutes")).toBe("Every few minutes");
    expect(cadenceLabel("realtime")).toBe("Live");
    expect(cadenceLabel("static")).toBe("Rarely changes");
  });

  it("falls back to the provider's own schedule for unknown or missing cadence", () => {
    expect(cadenceLabel(undefined)).toBe("Whenever the provider publishes");
    expect(cadenceLabel("fortnightly")).toBe("Whenever the provider publishes");
  });

  it("formats a check timestamp as a short date", () => {
    expect(checkedOnLabel("2026-09-28T04:10:43Z", "en-US")).toMatch(/^Sep 2[78], 2026$/);
  });

  it("returns empty for a missing or broken timestamp", () => {
    expect(checkedOnLabel(undefined)).toBe("");
    expect(checkedOnLabel("not a date")).toBe("");
  });
});
