// @vitest-environment jsdom
//
// CardShell's footer with and without the Round 20 interpretation line (ni-format §36).
// The component is mounted for real (vitest.config.ts compiles .svelte with its CSS
// injected), so every assertion reads the DOM nodes and jsdom's computed style.

import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it } from "vitest";

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
