import { describe, expect, it } from "vitest";
import { isStale, relTime } from "./time";

// parseTs treats a naive "YYYY-MM-DD HH:MM:SS[.ffffff]" as UTC — every helper below
// is anchored to a fixed `now` so nothing depends on the wall clock.
const NOW = new Date("2026-09-08T12:00:00Z");

function ago(ms: number): string {
  return new Date(NOW.getTime() - ms).toISOString().replace("T", " ").replace("Z", "");
}

describe("relTime", () => {
  it("dashes for a null or unparseable timestamp", () => {
    expect(relTime(null, NOW)).toBe("—");
    expect(relTime("not-a-date", NOW)).toBe("—");
  });

  it("reads a fresh timestamp as 'just now' inside the 45s window", () => {
    expect(relTime(ago(10_000), NOW)).toBe("just now");
  });

  it("minutes for < 1h", () => {
    expect(relTime(ago(3 * 60_000), NOW)).toBe("3m ago");
  });

  it("hours for < 24h", () => {
    expect(relTime(ago(5 * 3_600_000), NOW)).toBe("5h ago");
  });

  it("yesterday between 24h and 48h", () => {
    expect(relTime(ago(30 * 3_600_000), NOW)).toBe("yesterday");
  });

  it("days beyond 48h", () => {
    expect(relTime(ago(5 * 86_400_000), NOW)).toBe("5d ago");
  });

  it("never emits 'in 5m' on clock skew — reads as 'just now'", () => {
    // A negative delta (device clock ahead of server) reads as 'just now' rather than a
    // nonsense future tense that makes the card look broken.
    const future = new Date(NOW.getTime() + 60_000).toISOString().replace("T", " ").replace("Z", "");
    expect(relTime(future, NOW)).toBe("just now");
  });
});

describe("isStale", () => {
  it("false when there's no payload yet (card state carries the story)", () => {
    expect(isStale(null, 5, NOW)).toBe(false);
  });

  it("false when payload is younger than 2x cadence", () => {
    expect(isStale(ago(8 * 60_000), 5, NOW)).toBe(false);
  });

  it("true when payload is older than 2x cadence", () => {
    expect(isStale(ago(11 * 60_000), 5, NOW)).toBe(true);
  });

  it("clamps interval_minutes to 1 so 0/negative doesn't render everything stale/fresh", () => {
    expect(isStale(ago(3 * 60_000), 0, NOW)).toBe(true); // 3m > 2*1m
    expect(isStale(ago(30_000), -10, NOW)).toBe(false); // 30s < 2*1m
  });
});
