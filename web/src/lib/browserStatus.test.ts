// The plain words of Settings → Status "Page browser" (Round 20, B3b). The reason table is
// closed against the server's own sets — browsers.REASONS (the eligibility gate) and the
// Status row's blocker / wall / tier codes — and the phase words against install.PHASES; a
// code outside them (a newer server) reads as the neutral sentence, never an invented one.

import { describe, expect, it } from "vitest";

import type { BrowserEngineStatus } from "$lib/api";
import {
  engineChip,
  engineInMotion,
  engineMeta,
  engineReason,
  healthChip,
  READY_SENTENCE,
  REASON_SENTENCES,
  sandboxLine,
  UNKNOWN_SENTENCE,
  workerChip,
} from "./browserStatus";

function row(overrides: Partial<BrowserEngineStatus>): BrowserEngineStatus {
  return {
    name: "obscura", phase: "absent", pct: 0, version: "0.2.4", age_days: 12, eligible: false,
    reason: "", sandbox: { kind: "none", verified_at: null }, last_self_test: "never", error: "",
    health: "ok", ...overrides,
  };
}

// browsers/__init__.py REASONS (minus "") — every code the server can name.
const SERVER_REASONS = ["disabled", "manifest", "platform", "missing_libs", "sandbox", "userns_blocked",
  "apparmor_userns", "docker_no_sidecar", "tier_disabled", "absent", "stale", "preparing", "error", "self_test"];
// browsers/install.py PHASES.
const SERVER_PHASES = ["absent", "downloading", "verifying", "unpacking", "self-testing", "ready",
  "unavailable", "error", "stale", "disabled"] as const;

describe("engineReason — why the page browser can or cannot read pages here", () => {
  it("has its own sentence for every reason code the server sends, and nothing else", () => {
    expect(Object.keys(REASON_SENTENCES).sort()).toEqual([...SERVER_REASONS].sort());
    for (const reason of SERVER_REASONS) {
      const sentence = engineReason(row({ phase: "unavailable", reason }));
      expect(sentence, reason).toBe(REASON_SENTENCES[reason]);
      expect(sentence.endsWith("."), reason).toBe(true);
    }
    expect(new Set(Object.values(REASON_SENTENCES)).size).toBe(SERVER_REASONS.length);
  });

  it("reads an unknown code (a newer server) as the neutral sentence", () => {
    expect(engineReason(row({ reason: "quantum_flux" }))).toBe(UNKNOWN_SENTENCE);
    expect(engineReason(row({ reason: "constructor" }))).toBe(UNKNOWN_SENTENCE);
    expect(engineReason(row({ phase: "warp" as BrowserEngineStatus["phase"] }))).toBe(UNKNOWN_SENTENCE);
  });

  it("says ready only for an eligible engine", () => {
    expect(engineReason(row({ phase: "ready", eligible: true }))).toBe(READY_SENTENCE);
    expect(engineReason(row({ phase: "ready" }))).toBe(REASON_SENTENCES.self_test);
  });

  it("explains a row with no reason code through its phase", () => {
    expect(engineReason(row({ phase: "absent" }))).toBe(REASON_SENTENCES.absent);
    expect(engineReason(row({ phase: "stale" }))).toBe(REASON_SENTENCES.stale);
    expect(engineReason(row({ phase: "downloading", pct: 40 }))).toBe(REASON_SENTENCES.preparing);
    expect(engineReason(row({ phase: "disabled" }))).toBe(REASON_SENTENCES.disabled);
    expect(engineReason(row({ phase: "error" }))).toBe(REASON_SENTENCES.error);
  });

  it("shows the installer's own fixed sentence for a failed install", () => {
    const fixed = "could not reach the page browser download (github.com) — check the network and retry";
    expect(engineReason(row({ phase: "error", error: fixed }))).toBe(fixed);
  });
});

describe("engineChip — one chip per engine row", () => {
  it("has words for every phase the server sends", () => {
    for (const phase of SERVER_PHASES) {
      expect(engineChip(row({ phase, pct: 40 })).label, phase).not.toBe("unknown");
    }
  });

  it("reads an eligible engine as ready, whatever the phase says", () => {
    expect(engineChip(row({ phase: "ready", eligible: true }))).toEqual({ kind: "ok", label: "ready" });
  });

  it("puts the percentage on a download and marks motion and trouble", () => {
    expect(engineChip(row({ phase: "downloading", pct: 43 }))).toEqual({ kind: "accent", label: "downloading 43%" });
    expect(engineChip(row({ phase: "self-testing", pct: 99 }))).toEqual({ kind: "accent", label: "checking" });
    expect(engineChip(row({ phase: "unavailable" })).kind).toBe("warn");
    expect(engineChip(row({ phase: "error" })).kind).toBe("danger");
    expect(engineChip(row({ phase: "warp" as BrowserEngineStatus["phase"] }))).toEqual({ kind: "", label: "unknown" });
  });

  it("shows motion only while the engine downloads, unpacks or checks itself", () => {
    expect(SERVER_PHASES.filter((phase) => engineInMotion(row({ phase })))).toEqual(
      ["downloading", "verifying", "unpacking", "self-testing"],
    );
  });
});

describe("healthChip — the page browser paused after repeated failures", () => {
  it("is a warn chip only while the engine's breaker is open", () => {
    const chip = healthChip(row({ health: "unhealthy" }));
    expect(chip).toMatchObject({ kind: "warn", label: "paused after repeated failures" });
    expect(chip?.title).toContain("tries again by itself");
  });

  it("is nothing for a healthy engine or a row without the field (an older server)", () => {
    expect(healthChip(row({ health: "ok" }))).toBeNull();
    expect(healthChip(row({ health: undefined }))).toBeNull();
  });
});

describe("engineMeta and sandboxLine", () => {
  it("names the pinned build and its age", () => {
    expect(engineMeta(row({ age_days: 12 }))).toBe("v0.2.4, released 12 days ago");
    expect(engineMeta(row({ age_days: 1 }))).toBe("v0.2.4, released yesterday");
    expect(engineMeta(row({ age_days: 0 }))).toBe("v0.2.4, released today");
    expect(engineMeta(row({ age_days: -1 }))).toBe("v0.2.4");
  });

  it("names the sandbox and when it passed its check, or that it has not yet", () => {
    const passed = sandboxLine(row({ sandbox: { kind: "seatbelt", verified_at: "2026-10-12T10:42:00Z" } }));
    expect(passed.startsWith("Sandbox: the macOS sandbox — passed its check ")).toBe(true);
    expect(passed).toContain("2026");
    expect(sandboxLine(row({ sandbox: { kind: "userns", verified_at: null } })))
      .toBe("Sandbox: Linux namespaces — not checked since SmartBrain started");
    expect(sandboxLine(row({ sandbox: { kind: "sidecar", verified_at: null } })))
      .toBe("Sandbox: the renderer container — not checked since SmartBrain started");
    expect(sandboxLine(row({ sandbox: { kind: "none", verified_at: null } }))).toBe("Sandbox: none available here");
    expect(sandboxLine(row({ sandbox: { kind: "toString", verified_at: null } }))).toBe("Sandbox: none available here");
  });

  it("reads the flow worker as reading a page or idle", () => {
    expect(workerChip({ rendered_cards: 2, drafts: 1, worker: "busy" })).toEqual({ kind: "accent", label: "reading a page" });
    expect(workerChip({ rendered_cards: 0, drafts: 0, worker: "idle" })).toEqual({ kind: "", label: "idle" });
  });
});
