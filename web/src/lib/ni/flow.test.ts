import { describe, expect, it } from "vitest";
import {
  AWAITING_SOURCE_PICK,
  flowStageLabel,
  isFlowActive,
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

  it("returns empty for terminal error states — caller renders friendlyErrorClass", () => {
    expect(flowStageLabel({ state: "failed", error: "fetch_failed" })).toBe("");
    expect(flowStageLabel({ state: "unsupported", error: "no_source" })).toBe("");
  });

  it("returns empty for null / undefined so the card falls through to payload rendering", () => {
    expect(flowStageLabel(null)).toBe("");
    expect(flowStageLabel(undefined)).toBe("");
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
