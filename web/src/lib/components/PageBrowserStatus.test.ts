// @vitest-environment jsdom
//
// Settings → Status "Page browser" (Round 20, B3b; ni-format §35). Mounted for real, like
// CardShell.test.ts, from rows shaped like /api/status/overview's `browsers[]` and
// `browsers_flow`. Every sentence comes from the closed tables in $lib/browserStatus.

import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it } from "vitest";

import type { BrowserEngineStatus, BrowsersFlow } from "$lib/api";
import { READY_SENTENCE, REASON_SENTENCES, UNKNOWN_SENTENCE } from "$lib/browserStatus";
import PageBrowserStatus from "./PageBrowserStatus.svelte";

function row(overrides: Partial<BrowserEngineStatus>): BrowserEngineStatus {
  return {
    name: "obscura", phase: "ready", pct: 100, version: "0.2.4", age_days: 12, eligible: true, reason: "",
    sandbox: { kind: "seatbelt", verified_at: "2026-10-12T10:42:00Z" }, last_self_test: "ok", error: "",
    health: "ok", ...overrides,
  };
}

const READY = row({});
const STEALTH = row({
  name: "obscura-stealth", phase: "absent", pct: 0, eligible: false, reason: "tier_disabled",
  sandbox: { kind: "seatbelt", verified_at: null }, last_self_test: "never",
});
const FLOW: BrowsersFlow = { rendered_cards: 3, drafts: 1, worker: "busy" };

const mounted: Array<ReturnType<typeof mount>> = [];

afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.replaceChildren();
});

/** Mount the block and return its card. */
function blockOf(engines: BrowserEngineStatus[], flow?: BrowsersFlow): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  mounted.push(mount(PageBrowserStatus, { target, props: { engines, flow } }));
  flushSync();
  const card = target.querySelector<HTMLElement>(".card");
  expect(card, "the block renders").not.toBeNull();
  expect(card?.querySelector("h2")?.textContent).toBe("Page browser");
  return card as HTMLElement;
}

/** The texts of one engine row: its name line, its phase chip, the health chip (only while
 *  paused after repeated failures, else ""), and its notes. */
function engineTexts(card: HTMLElement, index: number): { head: string; chip: string; health: string; notes: string[] } {
  const engine = card.querySelectorAll(".pb-engine")[index];
  expect(engine, `engine row ${index}`).toBeTruthy();
  const chips = engine.querySelectorAll(".pb-chips > .chip");
  expect(chips.length, "the phase chip, then at most the health chip").toBeGreaterThanOrEqual(1);
  expect(chips.length).toBeLessThanOrEqual(2);
  return {
    head: engine.querySelector(".pb-row > span")?.textContent ?? "",
    chip: chips[0].textContent?.trim() ?? "",
    health: chips[1]?.textContent?.trim() ?? "",
    notes: Array.from(engine.querySelectorAll(".pb-note"), (p) => p.textContent ?? ""),
  };
}

describe("PageBrowserStatus — one row per engine", () => {
  it("names each engine with its version and age, its phase chip, sandbox and reason", () => {
    const card = blockOf([READY, STEALTH], FLOW);
    expect(card.querySelectorAll(".pb-engine")).toHaveLength(2);
    const ready = engineTexts(card, 0);
    expect(ready.head).toBe("obscura v0.2.4, released 12 days ago");
    expect(ready.chip).toBe("ready");
    expect(ready.notes[0].startsWith("Sandbox: the macOS sandbox — passed its check ")).toBe(true);
    expect(ready.notes[1]).toBe(READY_SENTENCE);
    const stealth = engineTexts(card, 1);
    expect(stealth.chip).toBe("not downloaded");
    expect(stealth.notes).toEqual(["Sandbox: the macOS sandbox — not checked since SmartBrain started", REASON_SENTENCES.tier_disabled]);
  });

  it("shows install progress only while an engine downloads or checks itself", () => {
    expect(blockOf([READY]).querySelector("progress")).toBeNull();
    const card = blockOf([row({ phase: "downloading", pct: 43, eligible: false })]);
    const bars = card.querySelectorAll<HTMLProgressElement>("progress");
    expect(bars).toHaveLength(1);
    expect(bars[0].value).toBe(43);
    expect(bars[0].max).toBe(100);
    expect(bars[0].getAttribute("style")).toBeNull();
    expect(engineTexts(card, 0).chip).toBe("downloading 43%");
    expect(engineTexts(card, 0).notes[1]).toBe(REASON_SENTENCES.preparing);
  });

  it("says why in the closed table's words for every reason code, and neutrally for others", () => {
    for (const [reason, sentence] of Object.entries(REASON_SENTENCES)) {
      const card = blockOf([row({ phase: "unavailable", eligible: false, reason, sandbox: { kind: "none", verified_at: null } })]);
      expect(engineTexts(card, 0).notes, reason).toEqual(["Sandbox: none available here", sentence]);
    }
    const unknown = blockOf([row({ phase: "unavailable", eligible: false, reason: "warp_drive" })]);
    expect(engineTexts(unknown, 0).notes[1]).toBe(UNKNOWN_SENTENCE);
  });

  it("adds a 'paused after repeated failures' chip only while the engine is unhealthy", () => {
    const card = blockOf([row({ health: "unhealthy" }), STEALTH, row({ name: "older", health: undefined })]);
    const paused = engineTexts(card, 0);
    expect(paused.chip).toBe("ready");
    expect(paused.health).toBe("paused after repeated failures");
    const chip = card.querySelectorAll(".pb-engine")[0].querySelectorAll(".pb-chips > .chip")[1];
    expect(chip.classList.contains("warn")).toBe(true);
    expect(chip.getAttribute("title")).toContain("paused after repeated failures");
    expect(engineTexts(card, 1).health).toBe("");
    expect(engineTexts(card, 2).health).toBe("");
  });

  it("offers no control — the server exposes no Retry or Turn off for the page browser", () => {
    const card = blockOf([row({ phase: "error", eligible: false, error: "the page browser install failed — retry" })], FLOW);
    expect(card.querySelector("button, a, input, select")).toBeNull();
    expect(engineTexts(card, 0).notes[1]).toBe("the page browser install failed — retry");
  });
});

describe("PageBrowserStatus — the cards that use it", () => {
  it("counts rendered cards and drafts and says whether a page is being read", () => {
    const card = blockOf([READY], FLOW);
    const rows = Array.from(card.querySelectorAll(".pb-rows > .pb-row"), (r) => [
      r.children[0]?.textContent?.trim(),
      r.children[1]?.textContent?.trim(),
    ]);
    expect(rows).toEqual([
      ["Cards that read their page in it", "3"],
      ["Waiting for your Activate", "1"],
      ["Right now", "reading a page"],
    ]);
    const idle = blockOf([READY], { rendered_cards: 0, drafts: 0, worker: "idle" });
    expect(idle.querySelector(".pb-rows > .pb-row:last-child .chip")?.textContent?.trim()).toBe("idle");
  });

  it("shows no counts without the flow field (locked, or an older server)", () => {
    expect(blockOf([READY]).querySelectorAll(".pb-rows > .pb-row")).toHaveLength(0);
  });
});
