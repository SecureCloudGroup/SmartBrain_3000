import { describe, expect, it } from "vitest";

import { cardText, withoutCardText, type CardText } from "./cardText";

describe("per-card NI text", () => {
  it("keeps two cards' values separate", () => {
    const key: CardText = {};
    key["a"] = "KEY-A";
    expect(cardText(key, "a")).toBe("KEY-A");
    expect(cardText(key, "b")).toBe("");
    key["b"] = "KEY-B";
    expect(cardText(key, "a")).toBe("KEY-A");
    expect(cardText(key, "b")).toBe("KEY-B");
  });

  it("clearing one card leaves the other intact", () => {
    const after = withoutCardText({ a: "KEY-A", b: "KEY-B" }, "a");
    expect(cardText(after, "a")).toBe("");
    expect(cardText(after, "b")).toBe("KEY-B");
  });
});
