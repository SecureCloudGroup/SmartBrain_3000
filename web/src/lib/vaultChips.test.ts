// Chip-state matrix for vaults: each shipped state renders a specific chip set. Pinning it
// here means a silent backend field rename (or a UI branch that quietly drops a chip)
// FAILS a test instead of turning up in production as a missing "Blocked" badge or a
// "Public" chip on a retired vault.

import { describe, expect, it } from "vitest";

import type { Vault } from "$lib/api";
import {
  retiredSubscriptionNote,
  unreachableSubscriptionNote,
  vaultChips,
  vaultChipsSummary,
} from "./vaultChips";

// A factory that fills every field the mapper reads, so a test only names the fields IT cares
// about. Defaults describe a plain LOCAL vault, never shared — the "Private" baseline.
function makeVault(overrides: Partial<Vault> = {}): Vault {
  const base: Vault = {
    id: "v-1",
    kind: "local",
    version: 1,
    internal_seq: 1,
    published_seq: null,
    published_at: null,
    retired_published: false,
    shared_sealed: false,
    sealed_seq: null,
    publisher_name: "",
    publisher_description: "",
    hosted_url: "",
    name: "Sample",
    description: "",
    tags: [],
    source: null,
    published_open: false,
    doc_count: 0,
    created_at: "",
    updated_at: "",
  };
  return { ...base, ...overrides };
}

describe("vaultChips — publisher-side states", () => {
  it("renders a positive Private chip when a local vault has never been shared", () => {
    const chips = vaultChips(makeVault());
    expect(chips).toHaveLength(1);
    expect(chips[0].label).toBe("Private");
    expect(chips[0].kind).toBe("");
  });

  it("renders Public v{published_seq} plus the publisher fingerprint on an open publish", () => {
    const chips = vaultChips(makeVault({
      published_open: true,
      published_seq: 3,
      internal_seq: 3,
      publisher_fingerprint: "SB-ABCD",
    }));
    expect(chips.map((c) => c.label)).toEqual(["Public v3", "SB-ABCD"]);
    expect(chips[0].kind).toBe("accent");
    expect(chips[1].mono).toBe(true);
  });

  it("falls back to internal_seq for legacy vaults published before published_seq existed", () => {
    // published_open true but published_seq still null — must show the seq the export bumped
    // (equal to internal_seq for a first open publish that predates the field).
    const chips = vaultChips(makeVault({
      published_open: true,
      published_seq: null,
      internal_seq: 4,
      publisher_fingerprint: "SB-EEEE",
    }));
    expect(chips[0].label).toBe("Public v4");
  });

  it("adds an Unpublished changes chip when internal_seq is ahead of published_seq", () => {
    const chips = vaultChips(makeVault({
      published_open: true,
      published_seq: 3,
      internal_seq: 5,
      publisher_fingerprint: "SB-FFFF",
    }));
    expect(chips.map((c) => c.label)).toEqual([
      "Public v3", "SB-FFFF", "Unpublished changes",
    ]);
    expect(chips[2].kind).toBe("warn");
  });

  it("replaces the Public accent chip with Retired v{n} when retired_published is set", () => {
    const chips = vaultChips(makeVault({
      published_open: true,
      published_seq: 7,
      internal_seq: 7,
      retired_published: true,
      publisher_fingerprint: "SB-ZZZZ",
    }));
    expect(chips.map((c) => c.label)).toEqual(["Retired v7", "SB-ZZZZ"]);
    expect(chips[0].kind).toBe("");
  });

  it("renders Shared · sealed for a sealed-only local vault", () => {
    const chips = vaultChips(makeVault({ shared_sealed: true, sealed_seq: 2 }));
    expect(chips).toHaveLength(1);
    expect(chips[0].label).toBe("Shared · sealed");
    expect(chips[0].kind).toBe("");
  });
});

describe("vaultChips — subscription states", () => {
  function subscribed(overrides: Partial<Vault> = {}): Vault {
    return makeVault({
      kind: "imported",
      source: { url: "https://ex.com/v.sbvault", seq: 2 },
      pinned_fingerprint: "SB-PIN1",
      ...overrides,
    });
  }

  it("renders the green Subscribed chip plus fingerprint + version", () => {
    const chips = vaultChips(subscribed());
    expect(chips.map((c) => c.label)).toEqual(["Subscribed", "SB-PIN1", "v2"]);
    expect(chips[0].kind).toBe("ok");
    expect(chips[0].icon).toBe("check");
  });

  it("REPLACES Subscribed with a danger Blocked chip when the pin is blocked", () => {
    const chips = vaultChips(subscribed({
      source: { url: "https://ex.com/v.sbvault", seq: 2, blocked: { offered_pubkey: "PK" } },
    }));
    expect(chips.map((c) => c.label)).toEqual(["Blocked", "SB-PIN1"]);
    expect(chips[0].kind).toBe("danger");
  });

  it("REPLACES Subscribed with Retired by publisher when source.retired is set", () => {
    const chips = vaultChips(subscribed({
      source: { url: "https://ex.com/v.sbvault", seq: 2, retired: true },
    }));
    expect(chips.map((c) => c.label)).toEqual(["Retired by publisher", "SB-PIN1"]);
    expect(chips[0].kind).toBe("");
  });

  it("REPLACES Subscribed with a warn Unreachable chip; reason drives the tooltip", () => {
    const dead = vaultChips(subscribed({
      source: { url: "https://ex.com/v.sbvault", seq: 2,
                unreachable: true, unreachable_reason: "dead_host" },
    }));
    expect(dead[0].label).toBe("Unreachable");
    expect(dead[0].kind).toBe("warn");
    expect(dead[0].title).toContain("week");

    const took = vaultChips(subscribed({
      source: { url: "https://ex.com/v.sbvault", seq: 2,
                unreachable: true, unreachable_reason: "took_down" },
    }));
    expect(took[0].title).toContain("took this vault down");
  });
});

describe("vaultChips — imported-from-file (not a URL subscription)", () => {
  it("renders Imported + the pinned fingerprint (matches subscribed identity chip)", () => {
    const chips = vaultChips(makeVault({
      kind: "imported",
      source: { vault_id: "abc" },
      pinned_fingerprint: "SB-IMPFP",
    }));
    expect(chips.map((c) => c.label)).toEqual(["Imported", "SB-IMPFP"]);
    expect(chips[1].mono).toBe(true);
  });
});

describe("vaultChipsSummary — the muted line beside the chip row", () => {
  it("returns a 'published {date}' line for a live subscription with a published_at", () => {
    const summary = vaultChipsSummary(makeVault({
      kind: "imported",
      source: { url: "https://ex.com/v.sbvault", seq: 2 },
      published_at: "2026-08-04",
    }));
    expect(summary).toBe("published 2026-08-04");
  });

  it("returns an empty string for a subscription with no publisher date", () => {
    expect(vaultChipsSummary(makeVault({
      kind: "imported",
      source: { url: "https://ex.com/v.sbvault", seq: 2 },
      published_at: null,
    }))).toBe("");
  });

  it("returns an empty string for a purely local vault (no publisher date to show)", () => {
    expect(vaultChipsSummary(makeVault({ published_at: "2026-08-04" }))).toBe("");
  });
});

describe("subscription-note helpers", () => {
  it("retiredSubscriptionNote fires only on a retired subscription", () => {
    expect(retiredSubscriptionNote(makeVault({
      kind: "imported",
      source: { url: "https://ex.com/v.sbvault", seq: 2, retired: true },
    }))).toContain("documents stay");
    expect(retiredSubscriptionNote(makeVault({
      kind: "imported",
      source: { url: "https://ex.com/v.sbvault", seq: 2 },
    }))).toBe("");
  });

  it("unreachableSubscriptionNote uses the reason to pick the copy", () => {
    expect(unreachableSubscriptionNote(makeVault({
      kind: "imported",
      source: { url: "https://ex.com/v.sbvault", seq: 2,
                unreachable: true, unreachable_reason: "took_down" },
    }))).toContain("publisher took this vault down");
    expect(unreachableSubscriptionNote(makeVault({
      kind: "imported",
      source: { url: "https://ex.com/v.sbvault", seq: 2,
                unreachable: true, unreachable_reason: "dead_host" },
    }))).toContain("hasn't answered for a week");
    expect(unreachableSubscriptionNote(makeVault({
      kind: "imported",
      source: { url: "https://ex.com/v.sbvault", seq: 2 },
    }))).toBe("");
  });
});
