// Overflow sentinel for IrPaint. The server laid every text prim out for its bucket with
// fontTools advance widths plus a margin; a text whose browser-measured scrollWidth still
// exceeds its `max_w` means the server's measure and the browser's paint disagree. The
// CSS already truncates with an ellipsis, so nothing breaks; this module marks the element
// and counts the event so tests (and a diagnostics surface later) can assert the count.

let count = 0;

export function getOverflowCount(): number {
  return count;
}

export function resetOverflowCount(): void {
  count = 0;
}

/** Mark and count every text prim under `root` whose rendered scrollWidth exceeds its
 *  laid-out `data-max-w`. Idempotent: the `data-ellipsis` attribute is sticky, so the
 *  pass that runs on every live tick never counts the same element twice. */
export function detectOverflows(root: HTMLElement): number {
  console.assert(root !== null && root !== undefined, "detectOverflows: root required");
  console.assert(typeof root.querySelectorAll === "function", "detectOverflows: root is an element");
  const nodes = root.querySelectorAll<HTMLElement>(".ni-t[data-max-w]");
  let found = 0;
  for (const el of nodes) {
    const max = Number(el.getAttribute("data-max-w"));
    if (!Number.isFinite(max) || max <= 0) continue;
    // 0.5 px swallows sub-pixel rounding between the layout's floats and the browser.
    if (el.scrollWidth > max + 0.5 && !el.hasAttribute("data-ellipsis")) {
      el.setAttribute("data-ellipsis", "");
      found += 1;
      count += 1;
    }
  }
  return found;
}
