// @vitest-environment jsdom
//
// CardShell's footer with and without the Round 20 interpretation line (ni-format §36), and
// the page browser's chip and state notice (§35, B3b).
// The component is mounted for real (vitest.config.ts compiles .svelte with its CSS
// injected), so every assertion reads the DOM nodes and jsdom's computed style.

import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it } from "vitest";

import type { FitText } from "$lib/ni/fit";
import type { CardState } from "$lib/ni/shell";
import CardShell from "./CardShell.svelte";

const FOOT = "as of 10:42 AM · every 5m";
const LINE = "Earthquakes · Magnitude > 4 · past 7 days · count";
const HTMLISH = '<img src="x" onerror="alert(1)"> Magnitude <b>4</b> · count';

const mounted: Array<ReturnType<typeof mount>> = [];

afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.replaceChildren();
});

/** Mount one non-form card (or a form card with `hasForm`) and return its footer row. */
function footOf(props: { interpretation?: string; hasForm?: boolean }): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  mounted.push(mount(CardShell, {
    target,
    props: { title: "Quakes", state: "stale", hasForm: props.hasForm ?? false, footerText: FOOT, ...props },
  }));
  flushSync();
  const foot = target.querySelector<HTMLElement>(".ni-foot");
  expect(foot, "the card renders its footer row").not.toBeNull();
  expect(target.querySelectorAll(".ni-foot")).toHaveLength(1);
  return foot as HTMLElement;
}

/** The single text node an element holds — the proof that a field reached the DOM as text. */
function onlyText(el: Element | null): string | null {
  expect(el, "element present").not.toBeNull();
  expect(el?.childNodes.length, "exactly one child node").toBe(1);
  const node = el?.firstChild;
  return node && node.nodeType === Node.TEXT_NODE ? node.nodeValue : null;
}

describe("CardShell footer — interpretation line", () => {
  it("keeps today's footer byte for byte without an interpretation (absent or empty)", () => {
    const absent = footOf({});
    const span = absent.firstElementChild;
    // Today's markup: one muted span holding the footer text (the class hash is Svelte's scoping).
    expect(span?.outerHTML).toMatch(/^<span class="muted ni-fresh svelte-[a-z0-9]+">as of 10:42 AM · every 5m<\/span>$/);
    expect(onlyText(span)).toBe(FOOT);
    expect(absent.querySelector(".ni-interp")).toBeNull();
    expect(footOf({ interpretation: "" }).outerHTML).toBe(absent.outerHTML);
  });

  it("leads with the interpretation, then the as-of · host · cadence segments", () => {
    const foot = footOf({ interpretation: LINE });
    const interp = foot.querySelector(".ni-fresh > .ni-interp");
    expect(onlyText(interp)).toBe(LINE);
    expect(interp?.getAttribute("title")).toBe(LINE);
    expect(onlyText(interp?.nextElementSibling ?? null)).toBe(`· ${FOOT}`);
    expect(foot.querySelector(".ni-fresh")?.textContent).toBe(`${LINE} · ${FOOT}`);
  });

  it("puts the line in as text, never as HTML", () => {
    const foot = footOf({ interpretation: HTMLISH });
    expect(foot.querySelector("img, b")).toBeNull();
    expect(onlyText(foot.querySelector(".ni-interp"))).toBe(HTMLISH);
    expect(foot.querySelector(".ni-interp")?.getAttribute("title")).toBe(HTMLISH);
  });

  it("cuts a 300-character line to one line with an ellipsis, the whole line in its title", () => {
    const long = `Earthquakes · ${"m".repeat(286)}`;
    expect(long).toHaveLength(300);
    const interp = footOf({ interpretation: long }).querySelector<HTMLElement>(".ni-interp");
    expect(interp?.getAttribute("title")).toBe(long);
    const style = getComputedStyle(interp as HTMLElement);
    expect(style.whiteSpace).toBe("nowrap");
    expect(style.textOverflow).toBe("ellipsis");
    expect(style.overflow).toBe("hidden");
  });

  it("adds nothing to a form card's footer — its CLIR paints the one footer", () => {
    const foot = footOf({ interpretation: LINE, hasForm: true });
    expect(foot.querySelector(".ni-fresh")).toBeNull();
    expect(foot.querySelector(".ni-interp")).toBeNull();
    expect(foot.textContent?.trim()).toBe("");
  });
});

// Round 20 (B3b, ni-format §35): a card that reads its page in the page browser wears a small
// muted "page browser" chip; when the page browser was unavailable on this machine the card's
// state line says so — plain text, in any state, in the failing reason's place, never dimmed.
const UNAVAILABLE = "The page browser is unavailable on this machine, so this card can't update here.";

/** Mount one card and return its root. */
function cardOf(props: {
  state?: CardState;
  rendered?: boolean;
  notice?: string;
  reason?: string;
  hasForm?: boolean;
  fit?: FitText | null;
}): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  mounted.push(mount(CardShell, {
    target,
    props: { title: "Gas prices", state: "fresh", hasForm: false, footerText: FOOT, ...props },
  }));
  flushSync();
  const card = target.querySelector<HTMLElement>(".ni-card");
  expect(card, "the card renders").not.toBeNull();
  expect(target.querySelectorAll(".ni-card")).toHaveLength(1);
  return card as HTMLElement;
}

describe("CardShell — the page browser chip and state notice", () => {
  it("adds one muted 'page browser' chip to the head chips of a rendered card", () => {
    const chips = cardOf({ rendered: true }).querySelectorAll(".ni-head .ni-chips .chip");
    expect(chips).toHaveLength(1);
    expect(chips[0].textContent?.trim()).toBe("page browser");
    // Chip's muted look is its kind "" — no accent/ok/warn/danger class.
    for (const kind of ["accent", "ok", "warn", "danger"]) expect(chips[0].classList.contains(kind)).toBe(false);
    expect(chips[0].getAttribute("title")).toContain("page browser");
  });

  it("adds no chip to any other card", () => {
    expect(cardOf({}).querySelector(".chip")).toBeNull();
    expect(cardOf({ rendered: false }).querySelector(".chip")).toBeNull();
  });

  it("says the page browser is unavailable as the state line, as text, in any state", () => {
    for (const state of ["fresh", "stale", "empty", "verifying", "designing"] as const) {
      const card = cardOf({ state, notice: UNAVAILABLE });
      expect(onlyText(card.querySelector(".ni-state-note"))).toBe(UNAVAILABLE);
      expect(card.querySelector(".ni-fail-reason")).toBeNull();
    }
    const html = '<b>x</b> unavailable';
    const card = cardOf({ notice: html });
    expect(card.querySelector(".ni-state-note b")).toBeNull();
    expect(onlyText(card.querySelector(".ni-state-note"))).toBe(html);
  });

  it("takes the failing reason line's place, outside the dimmed body", () => {
    const card = cardOf({ state: "failing", reason: "render_unavailable · last tried 2m ago", notice: UNAVAILABLE });
    expect(card.querySelector(".ni-fail-reason")).toBeNull();
    const note = card.querySelector(".ni-state-note");
    expect(onlyText(note)).toBe(UNAVAILABLE);
    expect(note?.closest(".ni-body")).toBeNull();
    expect(getComputedStyle(note as HTMLElement).opacity).not.toBe("0.6");
  });

  it("keeps the failing reason line when there is no notice, and adds nothing to a healthy card", () => {
    const failing = cardOf({ state: "failing", reason: "couldn't reach the source · last tried 2m ago" });
    expect(onlyText(failing.querySelector(".ni-fail-reason"))).toBe("couldn't reach the source · last tried 2m ago");
    expect(failing.querySelector(".ni-state-note")).toBeNull();
    expect(cardOf({ state: "fresh" }).querySelector(".ni-state-note, .ni-fail-reason")).toBeNull();
  });

  it("keeps the legacy chrome for a form card carrying a notice (the line sits under the body)", () => {
    expect(cardOf({ state: "fresh", hasForm: true }).classList.contains("form")).toBe(true);
    expect(cardOf({ state: "fresh", hasForm: true, notice: UNAVAILABLE }).classList.contains("form")).toBe(false);
  });
});

// Fit advisory (ruling 2026-10-12, $lib/ni/fit fitText; fix round 2026-10-13: a rendered check
// found the overlaid head-row chip covering painted content on tight-chrome cards). Legacy chrome
// (including a bound-form card NOT in tight chrome, e.g. "failing") wears it in the head's chip
// row, as before; tight/"form" chrome (CardShell's own `box` gate, same one formBoxChrome decides
// — not raw hasForm) wears it in the foot strip beside the ⋯ instead.
const FIT_NO: FitText = { chip: "May not answer your question", detail: null };
const FIT_PARTLY: FitText = { chip: "May not fully answer your question", detail: "Missing: wind, tide" };

describe("CardShell — fit advisory, legacy chrome (ruling 2026-10-12)", () => {
  it("adds nothing without a fit advisory (absent or null)", () => {
    expect(cardOf({}).querySelector(".ni-fit, .ni-fit-strip")).toBeNull();
    expect(cardOf({ fit: null }).querySelector(".ni-fit, .ni-fit-strip")).toBeNull();
  });

  it("wears a warn chip in the head, titled with the chip alone when there is no detail", () => {
    const chip = cardOf({ fit: FIT_NO }).querySelector<HTMLElement>(".ni-fit .chip");
    expect(chip?.classList.contains("warn")).toBe(true);
    expect(chip?.textContent?.trim()).toBe(FIT_NO.chip);
    expect(chip?.getAttribute("title")).toBe(FIT_NO.chip);
  });

  it("titles the head chip with the chip words AND the detail when one is named", () => {
    const chip = cardOf({ fit: FIT_PARTLY }).querySelector<HTMLElement>(".ni-fit .chip");
    expect(chip?.textContent?.trim()).toBe(FIT_PARTLY.chip);
    expect(chip?.getAttribute("title")).toBe(`${FIT_PARTLY.chip}. ${FIT_PARTLY.detail}`);
  });

  it("sits first in the head's chip row, ahead of the page browser chip", () => {
    const chips = cardOf({ fit: FIT_NO, rendered: true }).querySelectorAll(".ni-head .ni-chips > *");
    expect(chips[0].classList.contains("ni-fit")).toBe(true);
    expect(chips[1].textContent?.trim()).toBe("page browser");
  });

  it("shows in the head on a non-form card, and a form card NOT in tight chrome (e.g. failing)", () => {
    expect(cardOf({ fit: FIT_NO, hasForm: false }).querySelector(".ni-fit")).not.toBeNull();
    const failing = cardOf({ fit: FIT_NO, hasForm: true, state: "failing" });
    expect(failing.classList.contains("form")).toBe(false); // sanity: failing never gets tight chrome
    expect(failing.querySelector(".ni-fit")).not.toBeNull();
    expect(failing.querySelector(".ni-fit-strip")).toBeNull();
  });

  it("shows the detail as a second footer line only when one is named", () => {
    expect(cardOf({ fit: FIT_NO }).querySelector(".ni-fit-line")).toBeNull();
    const line = cardOf({ fit: FIT_PARTLY }).querySelector(".ni-fit-line");
    expect(onlyText(line)).toBe(FIT_PARTLY.detail);
  });

  it("shows for live and stale states alike", () => {
    for (const state of ["fresh", "stale"] as const) {
      expect(cardOf({ state, fit: FIT_NO }).querySelector(".ni-fit"), state).not.toBeNull();
    }
  });
});

describe("CardShell — fit advisory, tight (form) chrome (fix round 2026-10-13)", () => {
  it("wears it in the foot strip, never the head, when the card is tight chrome", () => {
    const card = cardOf({ fit: FIT_NO, hasForm: true, state: "fresh" });
    expect(card.classList.contains("form")).toBe(true); // sanity: this really is tight chrome
    expect(card.querySelector(".ni-head .ni-fit")).toBeNull();
    const chip = card.querySelector<HTMLElement>(".ni-foot .ni-fit-strip .chip");
    expect(chip?.classList.contains("warn")).toBe(true);
  });

  it("titles the strip's chip with the full sentence, same as the legacy chip", () => {
    const chip = cardOf({ fit: FIT_PARTLY, hasForm: true, state: "fresh" })
      .querySelector<HTMLElement>(".ni-fit-strip .chip");
    expect(chip?.getAttribute("title")).toBe(`${FIT_PARTLY.chip}. ${FIT_PARTLY.detail}`);
  });

  it("carries the chip words alone, and the chip+detail combined, as the strip's two texts", () => {
    const card = cardOf({ fit: FIT_PARTLY, hasForm: true, state: "fresh" });
    expect(onlyText(card.querySelector(".ni-fit-strip-short"))).toBe(FIT_PARTLY.chip);
    expect(onlyText(card.querySelector(".ni-fit-strip-phone"))).toBe(`${FIT_PARTLY.chip} · ${FIT_PARTLY.detail}`);
  });

  it("names the chip alone as both texts when there is no detail (nothing to fold in)", () => {
    const card = cardOf({ fit: FIT_NO, hasForm: true, state: "fresh" });
    expect(onlyText(card.querySelector(".ni-fit-strip-short"))).toBe(FIT_NO.chip);
    expect(onlyText(card.querySelector(".ni-fit-strip-phone"))).toBe(FIT_NO.chip);
  });

  it("never adds a separate footer detail line (folded into the strip's phone text instead)", () => {
    expect(cardOf({ fit: FIT_PARTLY, hasForm: true, state: "fresh" }).querySelector(".ni-fit-line")).toBeNull();
  });

  it("shows for fresh, stale and paused alike (CardShell's own tight-chrome states)", () => {
    for (const state of ["fresh", "stale", "paused"] as const) {
      expect(cardOf({ state, fit: FIT_NO, hasForm: true }).querySelector(".ni-fit-strip"), state).not.toBeNull();
    }
  });
});
