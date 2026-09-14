import { describe, expect, it } from "vitest";
import {
  AWAITING_SOURCE_CONFIRM,
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
      .toBe("Waiting for you to pick a source in chat");
    expect(flowStageLabel({ state: "source", error: "something_else" }))
      .toBe("Finding the source…");
  });

  // C3 (audit 2026-09-13): a recipe-matched flow pauses in confirm_source with the
  // AWAITING_SOURCE_CONFIRM marker until confirm_ni_flow_source lands the approval.
  it("labels the new confirm_source state distinctly from the pick-a-source pause", () => {
    expect(flowStageLabel({ state: "confirm_source", error: AWAITING_SOURCE_CONFIRM }))
      .toBe("Waiting for you to approve the source");
    // Marker absent (older backend) still renders the same sentence — the state name
    // alone is enough to distinguish this from every other stage.
    expect(flowStageLabel({ state: "confirm_source" }))
      .toBe("Waiting for you to approve the source");
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
  it("is true for every progressing state (including awaiting_credential + confirm_source)", () => {
    expect(isFlowActive({ state: "intent" })).toBe(true);
    expect(isFlowActive({ state: "source" })).toBe(true);
    expect(isFlowActive({ state: "confirm_source" })).toBe(true);
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
