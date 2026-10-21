import { describe, expect, it } from "vitest";
import {
  AWAITING_SOURCE_PICK,
  awaitingYesButtons,
  awaitingYesSource,
  flowStageLabel,
  hasAction,
  rowActions,
  isFlowActive,
  linkWhat,
  pickRows,
} from "./flow";

describe("flowStageLabel", () => {
  it("maps every progressing stage to its human sentence", () => {
    expect(flowStageLabel({ state: "intent" })).toBe("Understanding your request…");
    expect(flowStageLabel({ state: "source" })).toBe("Finding the source…");
    expect(flowStageLabel({ state: "sampling" })).toBe("Reading a sample…");
    expect(flowStageLabel({ state: "mapping" })).toBe("Choosing the data fields…");
    expect(flowStageLabel({ state: "assembling" })).toBe("Building the card…");
    expect(flowStageLabel({ state: "awaiting_credential" })).toBe("Needs your API key");
    expect(flowStageLabel({ state: "ready" })).toBe("Building the card…");
  });

  it("splits the 'source' state into search vs waiting-for-pick via flow.error", () => {
    expect(flowStageLabel({ state: "source", error: AWAITING_SOURCE_PICK }))
      .toBe("Waiting for you to pick a source on the card");
    expect(flowStageLabel({ state: "source", error: "something_else" }))
      .toBe("Finding the source…");
  });

  it("names the access pause", () => {
    expect(flowStageLabel({ state: "awaiting_access" })).toBe("Needs your key or contact email");
  });

  it("returns empty for terminal error states — caller renders friendlyErrorClass", () => {
    expect(flowStageLabel({ state: "failed", error: "fetch_failed" })).toBe("");
    expect(flowStageLabel({ state: "unsupported", error: "no_source" })).toBe("");
  });

  it("returns empty for null / undefined so the card falls through to payload rendering", () => {
    expect(flowStageLabel(null)).toBe("");
    expect(flowStageLabel(undefined)).toBe("");
  });

  // Round 20 (B3a/B3b): the waiting state — while a build waits for the page browser's first
  // install or check, the card's one line is the preparing line with its percentage; nothing
  // else about the card changes, and a settled phase leaves the stage label alone.
  it("shows 'Preparing the page browser (NN%)…' while the page browser gets ready", () => {
    expect(flowStageLabel({ state: "sampling", browser: { phase: "downloading", pct: 43 } }))
      .toBe("Preparing the page browser (43%)…");
    expect(flowStageLabel({ state: "sampling", browser: { phase: "self-testing", pct: 99 } }))
      .toBe("Preparing the page browser (99%)…");
    expect(flowStageLabel({ state: "sampling", browser: { phase: "ready", pct: 100 } }))
      .toBe("Reading a sample…");
  });

  it("never lets a stale browser field mask a terminal state", () => {
    expect(flowStageLabel({ state: "failed", error: "worker", browser: { phase: "downloading", pct: 5 } })).toBe("");
    expect(flowStageLabel({ state: "unsupported", browser: { phase: "verifying", pct: 50 } })).toBe("");
  });
});

describe("isFlowActive", () => {
  it("is true for every progressing state (including awaiting_credential)", () => {
    expect(isFlowActive({ state: "intent" })).toBe(true);
    expect(isFlowActive({ state: "source" })).toBe(true);
    expect(isFlowActive({ state: "sampling" })).toBe(true);
    expect(isFlowActive({ state: "mapping" })).toBe(true);
    expect(isFlowActive({ state: "assembling" })).toBe(true);
    expect(isFlowActive({ state: "awaiting_credential" })).toBe(true);
    expect(isFlowActive({ state: "ready" })).toBe(true);
  });

  it("is false for terminal error states (fast-poll would spin forever)", () => {
    expect(isFlowActive({ state: "failed" })).toBe(false);
    expect(isFlowActive({ state: "unsupported" })).toBe(false);
  });

  it("is false for a null / undefined flow", () => {
    expect(isFlowActive(null)).toBe(false);
    expect(isFlowActive(undefined)).toBe(false);
  });
});

it("awaiting_params labels as a needed detail (needs_params 2026-09-14)", () => {
  expect(flowStageLabel({ state: "awaiting_params" })).toBe(
    "Needs a detail from you",
  );
});

describe("awaitingYesSource (ruling 2026-10-04: hold open paths for a YES)", () => {
  it("names the host and the page or dataset title", () => {
    expect(awaitingYesSource({ from: "page", host: "www.nhc.noaa.gov", title: "NHC Outlook" }))
      .toBe("From the web page www.nhc.noaa.gov — NHC Outlook");
    expect(awaitingYesSource({ from: "dataset", host: "data.cdc.gov", title: "Flu levels by state" }))
      .toBe("From the dataset data.cdc.gov — Flu levels by state");
  });

  it("leaves off a missing title or one that only repeats the host", () => {
    expect(awaitingYesSource({ from: "dataset", host: "api.example.org", title: "" }))
      .toBe("From the dataset at api.example.org");
    expect(awaitingYesSource({ from: "page", host: "example.org", title: " Example.org " }))
      .toBe("From the web page at example.org");
  });

  it("never renders an empty host", () => {
    expect(awaitingYesSource({ from: "page", host: "", title: "Tides" })).toBe("From the web page “Tides”");
    expect(awaitingYesSource({ from: "dataset", host: "", title: "" })).toBe("From a dataset");
  });
});

describe("pickRows (ruling 2026-10-05: Library cards; web as links)", () => {
  const lib = { kind: "library" as const, title: "USGS gauge", host: "waterservices.usgs.gov",
                url: "https://waterservices.usgs.gov/x" };
  const dataset = { kind: "link" as const, found: "dataset" as const, title: "Gauges", host: "data.ny.gov",
                    url: "https://data.ny.gov/d/abcd" };
  const page = { kind: "link" as const, found: "page" as const, title: "River levels", host: "rivers.example.org",
                 url: "https://rivers.example.org/now" };

  it("taps Library sources and offers the rest as links", () => {
    const out = pickRows([lib, dataset, page]);
    expect(out.sources).toEqual([lib]);
    expect(out.links).toEqual([dataset, page]);
    expect(out.heading).toBe("From the SmartBrain Library — tap the one that fits:");
  });

  it("says plainly when no Library source answers", () => {
    expect(pickRows([page]).heading).toBe("No SmartBrain Library source answers this yet.");
    expect(pickRows([]).heading)
      .toBe("No SmartBrain Library source answers this yet — paste a link to the data:");
    expect(pickRows(undefined).sources).toEqual([]);
  });

  it("never offers an unknown kind as a tap, nor a link that isn't https", () => {
    const out = pickRows([{ ...page, kind: undefined }, { ...page, url: "javascript:alert(1)" }]);
    expect(out.sources).toEqual([]);
    expect(out.links).toEqual([]);
  });

  it("lists each address once", () => {
    const out = pickRows([lib, { ...lib }, dataset, { ...dataset, title: "Other dataset" }]);
    expect(out.sources).toEqual([lib]);
    expect(out.links).toEqual([dataset]);
  });

  it("names where a link goes and what it is", () => {
    expect(linkWhat(dataset)).toBe("data.ny.gov · dataset");
    expect(linkWhat(page)).toBe("rivers.example.org · web page");
    expect(linkWhat({ ...page, host: " " })).toBe("web page");
  });
});

describe("rowActions / hasAction", () => {
  it("is null for a v1 row (no actions key) and hasAction is false", () => {
    expect(rowActions({})).toBeNull();
    expect(rowActions(null)).toBeNull();
    expect(hasAction({ flow: { state: "failed" } as never }, "retry")).toBe(false);
  });

  it("reads the row's actions and ignores unknown kinds", () => {
    const row = { actions: ["run", "from_the_future", "delete"] };
    expect(rowActions(row)).toEqual(["run", "delete"]);
    expect(hasAction(row, "run")).toBe(true);
    expect(hasAction(row, "from_the_future")).toBe(false);
  });

  it("prefers the flow's own actions while a flow is present", () => {
    const row = { actions: ["run"], flow: { actions: ["cancel"] } };
    expect(rowActions(row)).toEqual(["cancel"]);
    expect(hasAction(row, "cancel")).toBe(true);
    expect(hasAction(row, "run")).toBe(false);
  });

  it("an empty list is a v2 row with no buttons, not a v1 row", () => {
    expect(rowActions({ actions: [] })).toEqual([]);
    expect(hasAction({ actions: [] }, "keep")).toBe(false);
  });
});

describe("flowStageLabel with progress", () => {
  it("uses the backend progress label when present", () => {
    const progress = { step: 2, of: 4, label: "Finding a source" };
    expect(flowStageLabel({ state: "source", progress })).toBe("Finding a source");
    expect(flowStageLabel({ state: "sampling", progress: { ...progress, step: 3, label: "Reading a sample" } }))
      .toBe("Reading a sample");
  });

  it("keeps the v1 labels without progress and never labels a terminal flow", () => {
    expect(flowStageLabel({ state: "source" })).toBe("Finding the source…");
    expect(flowStageLabel({ state: "failed", progress: { step: 1, of: 4, label: "x" } })).toBe("");
  });
});

describe("awaiting-yes wording for from: library", () => {
  it("names the Library source with its host", () => {
    expect(awaitingYesSource({ from: "library", host: "data.cityofchicago.org", title: "Crimes" }))
      .toBe("From the SmartBrain Library: Crimes (data.cityofchicago.org)");
    expect(awaitingYesSource({ from: "library", host: "api.nasa.gov", title: "" }))
      .toBe("From the SmartBrain Library: api.nasa.gov");
    expect(awaitingYesSource({ from: "library", host: "", title: "" })).toBe("From the SmartBrain Library");
  });

  it("leaves the page and dataset sentences alone", () => {
    expect(awaitingYesSource({ from: "page", host: "a.com", title: "T" })).toBe("From the web page a.com — T");
    expect(awaitingYesSource({ from: "dataset", host: "a.com", title: "" })).toBe("From the dataset at a.com");
  });

  it("reads Keep / Try another source only for library", () => {
    expect(awaitingYesButtons({ from: "library", host: "h", title: "t" }))
      .toEqual({ yes: "Keep", yesBusy: "Keeping…", no: "Try another source" });
    expect(awaitingYesButtons({ from: "page", host: "h", title: "t" }).yes).toBe("Yes, that’s it");
    expect(awaitingYesButtons({ from: "dataset", host: "h", title: "t" }).no).toBe("No, try another source");
  });
});
