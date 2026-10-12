// @vitest-environment jsdom
//
// VerifyPanel's "Reads as" line and coverage chips (Round 20, ni-format §36). Mounted for
// real, like CardShell.test.ts. The payload is a bound form whose CLIR faces are opaque
// stubs: IrPaint shows its "unrenderable" placeholder for them, which is all these tests
// need from the preview.

import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it } from "vitest";

import type { NiCoverage } from "$lib/api";
import VerifyPanel from "./VerifyPanel.svelte";

const LINE = "Earthquakes · Magnitude > 4 · past 7 days · count";
const HTMLISH = '<img src="x" onerror="alert(1)"> Magnitude <b>4</b> · count';
const PAYLOAD = { type: "form", form: "stat", clir: { desktop: { span: "d1x1" }, phone: { span: "p1x1" } } };

const mounted: Array<ReturnType<typeof mount>> = [];

afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.replaceChildren();
});

/** Mount the panel for one form payload and return its root. */
function panelOf(props: { interpretation?: string; coverage?: Record<string, NiCoverage> }): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  mounted.push(mount(VerifyPanel, {
    target,
    props: { payload: PAYLOAD, viewerTz: "UTC", onUse: () => {}, onWrong: () => {}, ...props },
  }));
  flushSync();
  const panel = target.querySelector<HTMLElement>(".verify-panel");
  expect(panel, "the panel renders").not.toBeNull();
  expect(panel?.querySelector(".ni-actions"), "the pick's controls render").not.toBeNull();
  return panel as HTMLElement;
}

/** The single text node an element holds — the proof that a field reached the DOM as text. */
function onlyText(el: Element | null): string | null {
  expect(el, "element present").not.toBeNull();
  expect(el?.childNodes.length, "exactly one child node").toBe(1);
  const node = el?.firstChild;
  return node && node.nodeType === Node.TEXT_NODE ? node.nodeValue : null;
}

/** The coverage chips' texts, in DOM order — every chip in the panel sits in the Reads as row. */
function chipTexts(panel: HTMLElement): string[] {
  const chips = Array.from(panel.querySelectorAll(".verify-reads .chip"));
  expect(panel.querySelectorAll(".chip"), "no chip outside the Reads as line").toHaveLength(chips.length);
  expect(chips.every((chip) => chip.matches(".verify-reads-chips > .chip")), "chips sit in their row").toBe(true);
  return chips.map((chip) => chip.textContent?.trim() ?? "");
}

describe("VerifyPanel — how the ask was read", () => {
  it("shows no line without an interpretation, whatever the coverage says", () => {
    expect(panelOf({}).querySelector(".verify-reads")).toBeNull();
    expect(panelOf({ coverage: { time: "unbound" } }).querySelector(".verify-reads")).toBeNull();
    expect(panelOf({ interpretation: "" }).querySelector(".chip")).toBeNull();
  });

  it("puts 'Reads as' and the line, verbatim, directly above the controls", () => {
    const panel = panelOf({ interpretation: LINE });
    const reads = panel.querySelector(".verify-reads");
    expect(onlyText(reads?.querySelector(".verify-reads-label") ?? null)).toBe("Reads as");
    expect(onlyText(reads?.querySelector(".verify-reads-text") ?? null)).toBe(LINE);
    expect(reads?.nextElementSibling).toBe(panel.querySelector(".ni-actions"));
    expect(chipTexts(panel)).toEqual([]);
  });

  it("appends a muted chip per approximated or unbound clause, in the line's order", () => {
    const panel = panelOf({
      interpretation: LINE,
      coverage: { time: "approximated", where: "unbound", select: "unbound", answer: "rules", agg: "bound" },
    });
    expect(chipTexts(panel)).toEqual(["quantity not found", "filter not found", "time approximated"]);
    for (const chip of panel.querySelectorAll(".verify-reads .chip")) {
      // Chip's muted look is its kind "" — no accent/ok/warn/danger class.
      expect(chip.classList.contains("accent") || chip.classList.contains("warn")).toBe(false);
      expect(chip.classList.contains("ok") || chip.classList.contains("danger")).toBe(false);
    }
  });

  it("puts the line in as text, never as HTML", () => {
    const panel = panelOf({ interpretation: HTMLISH });
    expect(panel.querySelector("img, b")).toBeNull();
    expect(onlyText(panel.querySelector(".verify-reads-text"))).toBe(HTMLISH);
  });

  it("shows a 300-character line whole — no cut, no single-line ellipsis", () => {
    const long = `Earthquakes · ${"m".repeat(286)}`;
    expect(long).toHaveLength(300);
    const text = panelOf({ interpretation: long }).querySelector<HTMLElement>(".verify-reads-text");
    expect(onlyText(text)).toBe(long);
    const style = getComputedStyle(text as HTMLElement);
    expect(style.whiteSpace).not.toBe("nowrap");
    expect(style.textOverflow).not.toBe("ellipsis");
  });
});
