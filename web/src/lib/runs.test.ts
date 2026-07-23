import { describe, expect, it } from "vitest";
import { daysLeft, localTs, runStatusLabel } from "./runs";

describe("localTs", () => {
  it("renders a UTC server timestamp in the local locale", () => {
    const out = localTs("2026-06-21 14:30:00");
    // The exact string is locale-dependent; it must parse and not echo the raw form.
    expect(out).not.toBe("");
    expect(out).toBe(new Date("2026-06-21T14:30:00Z").toLocaleString());
  });

  it("passes malformed input through unchanged", () => {
    expect(localTs("not a date")).toBe("not a date");
  });

  it("returns empty for null/empty", () => {
    expect(localTs(null)).toBe("");
    expect(localTs("")).toBe("");
  });
});

describe("runStatusLabel", () => {
  it("maps known statuses to human labels", () => {
    expect(runStatusLabel("awaiting_approval")).toBe("Needs approval");
    expect(runStatusLabel("error")).toBe("Failed");
    expect(runStatusLabel("complete")).toBe("Done");
  });

  it("passes unknown statuses through", () => {
    expect(runStatusLabel("running")).toBe("running");
  });
});

describe("daysLeft", () => {
  const now = new Date("2026-07-23T12:00:00Z");

  it("counts whole days remaining in the retention window", () => {
    expect(daysLeft("2026-07-23 09:00:00", 30, now)).toBe(30); // trashed today
    expect(daysLeft("2026-07-13 12:00:00", 30, now)).toBe(20); // 10 days ago
  });

  it("floors at zero once the window has lapsed", () => {
    expect(daysLeft("2026-06-01 00:00:00", 30, now)).toBe(0);
  });

  it("falls back to the full window on malformed timestamps", () => {
    expect(daysLeft("garbage", 30, now)).toBe(30);
  });
});
