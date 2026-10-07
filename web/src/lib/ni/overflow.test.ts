// @vitest-environment jsdom
//
// Overflow sentinel for IrPaint. Runs under jsdom (already a devDependency; the
// `check-node.mjs` guard pins the Node floor it needs). jsdom doesn't do real layout
// — scrollWidth returns 0 by default — so the test stubs scrollWidth per element.
// What we're pinning is the walk-and-mark logic, not font metrics.

import { afterEach, describe, expect, it } from "vitest";

import {
  detectOverflows,
  getOverflowCount,
  resetOverflowCount,
} from "./overflow";

function makeNode(maxW: number, scroll: number): HTMLElement {
  const el = document.createElement("div");
  el.className = "ni-t";
  el.setAttribute("data-max-w", String(maxW));
  Object.defineProperty(el, "scrollWidth", { value: scroll, configurable: true });
  return el;
}

afterEach(() => resetOverflowCount());

describe("detectOverflows", () => {
  it("marks and counts only the overflowing elements", () => {
    const root = document.createElement("div");
    root.appendChild(makeNode(100, 80));   // in-bounds
    root.appendChild(makeNode(100, 140));  // overflow
    root.appendChild(makeNode(80, 200));   // overflow
    document.body.appendChild(root);

    expect(detectOverflows(root)).toBe(2);
    expect(getOverflowCount()).toBe(2);

    const marked = root.querySelectorAll("[data-ellipsis]");
    expect(marked.length).toBe(2);
  });

  it("is idempotent — a second pass over the same nodes doesn't double-count", () => {
    const root = document.createElement("div");
    root.appendChild(makeNode(50, 120)); // overflow
    document.body.appendChild(root);

    expect(detectOverflows(root)).toBe(1);
    expect(detectOverflows(root)).toBe(0); // same elements, already marked
    expect(getOverflowCount()).toBe(1);
  });

  it("ignores elements without a valid data-max-w", () => {
    const root = document.createElement("div");
    const bad = document.createElement("div");
    bad.className = "ni-t";
    bad.setAttribute("data-max-w", "");
    Object.defineProperty(bad, "scrollWidth", { value: 9999, configurable: true });
    root.appendChild(bad);
    document.body.appendChild(root);

    expect(detectOverflows(root)).toBe(0);
    expect(getOverflowCount()).toBe(0);
  });

  it("ignores a 0.5 px tolerance (sub-pixel noise never counts)", () => {
    const root = document.createElement("div");
    root.appendChild(makeNode(100, 100.3));
    document.body.appendChild(root);

    expect(detectOverflows(root)).toBe(0);
  });
});
