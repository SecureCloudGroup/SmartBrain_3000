// Pure logic for the Neural Interface library sheet (search/filter, param-form
// validation, fingerprint format). The sheet has no way to reach these branches
// off the happy path except through these functions — so they pin the contract
// the /ni page depends on.

import { describe, expect, it } from "vitest";
import type { NiTemplate, NiTemplateParam } from "$lib/api";
import type { SceneNode } from "$lib/ni/scene";
import {
  filterTemplates,
  formatFingerprint,
  paramValuesForInstall,
  templateCategories,
  validateParamForm,
} from "./library";

// Minimal preview payload — a valid bound scene, kept trivial so the tests focus on
// the search/filter/validation logic, not scene shape.
const PREVIEW: SceneNode = { type: "stack", dir: "v", gap: "sm", children: [] };

function mk(
  id: string,
  title: string,
  category: string,
  tags: string[] = [],
  goal = "",
): NiTemplate {
  return {
    id,
    title,
    goal,
    category,
    tags,
    notes: "",
    sources: [],
    params: [],
    preview_payload: PREVIEW,
  };
}

const CATALOG: NiTemplate[] = [
  mk("t1", "AAPL price", "Finance", ["stock", "market"], "Latest AAPL quote every 5 minutes"),
  mk("t2", "SFO weather", "Weather", ["forecast"], "Current conditions at SFO"),
  mk("t3", "GitHub stars", "Dev", ["repo"], "Repo star count trend"),
];

describe("filterTemplates", () => {
  it("returns everything when query and category are empty", () => {
    expect(filterTemplates(CATALOG, "", "")).toHaveLength(3);
  });

  it("matches on title case-insensitively", () => {
    const r = filterTemplates(CATALOG, "aapl", "");
    expect(r.map((t) => t.id)).toEqual(["t1"]);
  });

  it("matches on goal text", () => {
    const r = filterTemplates(CATALOG, "trend", "");
    expect(r.map((t) => t.id)).toEqual(["t3"]);
  });

  it("matches on a tag", () => {
    const r = filterTemplates(CATALOG, "forecast", "");
    expect(r.map((t) => t.id)).toEqual(["t2"]);
  });

  it("matches on category name via the query field", () => {
    const r = filterTemplates(CATALOG, "finance", "");
    expect(r.map((t) => t.id)).toEqual(["t1"]);
  });

  it("filters by category exactly (case sensitive — matches the category strings the pack declares)", () => {
    // The category dropdown's values are drawn from templateCategories(), so exact
    // string equality is the right check (a user never types this field).
    const r = filterTemplates(CATALOG, "", "Weather");
    expect(r.map((t) => t.id)).toEqual(["t2"]);
  });

  it("composes query AND category filters", () => {
    // "stars" in title + Dev category — matches t3 only.
    const r = filterTemplates(CATALOG, "stars", "Dev");
    expect(r.map((t) => t.id)).toEqual(["t3"]);
    // Same query with the wrong category returns nothing (no cross-category leak).
    expect(filterTemplates(CATALOG, "stars", "Finance")).toEqual([]);
  });
});

describe("templateCategories", () => {
  it("returns unique categories sorted alphabetically (case-insensitive)", () => {
    const catalog = [
      mk("a", "A", "weather"),
      mk("b", "B", "Dev"),
      mk("c", "C", "Finance"),
      mk("d", "D", "Dev"),
    ];
    expect(templateCategories(catalog)).toEqual(["Dev", "Finance", "weather"]);
  });

  it("skips empty/whitespace categories rather than emitting a blank entry", () => {
    const catalog = [mk("a", "A", ""), mk("b", "B", "   "), mk("c", "C", "Dev")];
    expect(templateCategories(catalog)).toEqual(["Dev"]);
  });
});

describe("validateParamForm", () => {
  const PARAMS: NiTemplateParam[] = [
    { name: "ticker", label: "Ticker symbol", kind: "string" },
    { name: "interval", label: "Interval (minutes)", kind: "number" },
    { name: "api_key", label: "API key", kind: "secret" },
  ];

  it("passes when every required non-secret param is filled", () => {
    expect(validateParamForm(PARAMS, { ticker: "AAPL", interval: "5" })).toBeNull();
  });

  it("reports the first empty non-secret param by name (so the sheet can focus it)", () => {
    const err = validateParamForm(PARAMS, { ticker: "", interval: "5" });
    expect(err?.name).toBe("ticker");
    expect(err?.message).toMatch(/Ticker symbol/);
  });

  it("treats whitespace-only strings as empty", () => {
    const err = validateParamForm(PARAMS, { ticker: "   ", interval: "5" });
    expect(err?.name).toBe("ticker");
  });

  it("rejects a non-numeric value for a number param", () => {
    const err = validateParamForm(PARAMS, { ticker: "AAPL", interval: "soon" });
    expect(err?.name).toBe("interval");
    expect(err?.message).toMatch(/must be a number/);
  });

  it("ignores missing secret params — those are entered on the card after install (§20)", () => {
    // api_key is a secret; the form must submit without it.
    expect(validateParamForm(PARAMS, { ticker: "AAPL", interval: "5" })).toBeNull();
  });
});

describe("paramValuesForInstall", () => {
  const PARAMS: NiTemplateParam[] = [
    { name: "ticker", label: "Ticker", kind: "string" },
    { name: "interval", label: "Minutes", kind: "number" },
    { name: "api_key", label: "API key", kind: "secret" },
  ];

  it("coerces number params to number and drops secret params from the install body", () => {
    const out = paramValuesForInstall(PARAMS, { ticker: "AAPL", interval: "5", api_key: "leak" });
    expect(out).toEqual({ ticker: "AAPL", interval: 5 });
  });

  it("trims string values so the server sees what the user meant", () => {
    const out = paramValuesForInstall(PARAMS, { ticker: "  AAPL  ", interval: "5" });
    expect(out.ticker).toBe("AAPL");
  });
});

describe("formatFingerprint", () => {
  it("prefixes 'Publisher ' when a fingerprint is present", () => {
    expect(formatFingerprint("SB-ABCD-1234-EFGH")).toBe("Publisher SB-ABCD-1234-EFGH");
  });

  it("falls back to 'Unknown publisher' for null/empty/whitespace input (never a bare 'Publisher ')", () => {
    expect(formatFingerprint(null)).toBe("Unknown publisher");
    expect(formatFingerprint(undefined)).toBe("Unknown publisher");
    expect(formatFingerprint("")).toBe("Unknown publisher");
    expect(formatFingerprint("   ")).toBe("Unknown publisher");
  });
});
