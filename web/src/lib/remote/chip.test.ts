// chipState is the phone-side derivation that gives the header chip a distinct "locked"
// presentation on top of the bridge status. A regression here would return the app to
// conflating "Desktop is locked" with "Desktop is unreachable" — the exact bug this fixes.

import { describe, expect, it } from "vitest";

import { chipState } from "./chip";

describe("chipState (locked-vs-unreachable presentation)", () => {
  it("returns 'locked' when the bridge is connected AND the vault is locked", () => {
    expect(chipState("connected", false)).toBe("locked");
    expect(chipState("connected-direct", false)).toBe("locked");
    expect(chipState("connected-relay", false)).toBe("locked");
  });

  it("returns the underlying connected state when the vault is unlocked", () => {
    expect(chipState("connected", true)).toBe("connected");
    expect(chipState("connected-direct", true)).toBe("connected-direct");
    expect(chipState("connected-relay", true)).toBe("connected-relay");
  });

  it("does NOT invent 'locked' before account.status has loaded (null)", () => {
    expect(chipState("connected", null)).toBe("connected");
    expect(chipState("connected-direct", null)).toBe("connected-direct");
  });

  it("does NOT flip to 'locked' when the bridge is offline — a locked desktop still connects", () => {
    expect(chipState("offline", false)).toBe("offline");
    expect(chipState("reconnecting", false)).toBe("reconnecting");
    expect(chipState("connecting", false)).toBe("connecting");
    expect(chipState("verifying", false)).toBe("verifying");
  });

  it("passes non-connected statuses through unchanged regardless of unlocked", () => {
    expect(chipState("untrusted", false)).toBe("untrusted");
    expect(chipState("untrusted", true)).toBe("untrusted");
    expect(chipState("idle", true)).toBe("idle");
    expect(chipState("idle", null)).toBe("idle");
  });
});
