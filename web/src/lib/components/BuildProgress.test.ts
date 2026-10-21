// @vitest-environment jsdom
//
// BuildProgress — the v2 build line (step bar, auto-pick attempt, Cancel). Mounted for real,
// like FeedOffer.test.ts; the cancel call is injected so nothing touches the network.

import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { NiFlowProgress } from "$lib/api";
import BuildProgress from "./BuildProgress.svelte";

const mounted: Array<ReturnType<typeof mount>> = [];

afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.replaceChildren();
});

function barOf(props: {
  progress?: NiFlowProgress;
  canCancel?: boolean;
  cancel?: (id: string) => Promise<unknown>;
  onCancelled?: () => void;
}): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  mounted.push(mount(BuildProgress, {
    target,
    props: { itemId: "it1", progress: { step: 2, of: 4, label: "Finding a source" }, cancel: () => Promise.resolve({}), ...props },
  }));
  flushSync();
  const root = target.querySelector<HTMLElement>(".bp");
  expect(root, "the progress block renders").not.toBeNull();
  return root as HTMLElement;
}

const settle = async () => { await Promise.resolve(); await Promise.resolve(); flushSync(); };

describe("BuildProgress", () => {
  it("shows the label and a bar that says which step it is on", () => {
    const root = barOf({});
    expect(root.querySelector(".bp-label")?.textContent).toBe("Finding a source");
    const bar = root.querySelector("[role=progressbar]");
    expect(bar?.getAttribute("aria-valuenow")).toBe("2");
    expect(bar?.getAttribute("aria-valuemax")).toBe("4");
    expect(root.querySelectorAll(".bp-seg")).toHaveLength(4);
    expect(root.querySelectorAll(".bp-seg.done")).toHaveLength(2);
  });

  it("names the Library source being tried", () => {
    const root = barOf({
      progress: { step: 3, of: 4, label: "Reading a sample", try: { n: 2, of: 3, host: "data.ny.gov" } },
    });
    expect(root.querySelector(".bp-try")?.textContent).toBe("Trying source 2 of 3 (data.ny.gov)");
  });

  it("says nothing about attempts for a single source", () => {
    const root = barOf({ progress: { step: 3, of: 4, label: "x", try: { n: 1, of: 1, host: "h" } } });
    expect(root.querySelector(".bp-try")).toBeNull();
    expect(root.querySelectorAll("button")).toHaveLength(0);
  });

  it("offers Cancel only when the backend declared it", () => {
    expect(barOf({ canCancel: false }).querySelector("button")).toBeNull();
    const withCancel = barOf({ canCancel: true });
    expect(withCancel.querySelectorAll("button")).toHaveLength(1);
    expect(withCancel.querySelector("button")?.textContent).toBe("Cancel");
  });

  it("Cancel calls the injected cancel for this item, then tells the page", async () => {
    const cancel = vi.fn(() => Promise.resolve({ ok: true }));
    const onCancelled = vi.fn();
    const root = barOf({ canCancel: true, cancel, onCancelled });
    root.querySelector("button")?.click();
    flushSync();
    expect(root.querySelector("button")?.disabled).toBe(true);
    await settle();
    expect(cancel).toHaveBeenCalledWith("it1");
    expect(onCancelled).toHaveBeenCalledTimes(1);
    expect(root.querySelector("button")?.disabled).toBe(false);
    expect(root.querySelector(".bp-error")).toBeNull();
  });

  it("a refused cancel shows its reason on the card and does not refresh", async () => {
    const cancel = vi.fn(() => Promise.reject(new Error("nothing is building")));
    const onCancelled = vi.fn();
    const root = barOf({ canCancel: true, cancel, onCancelled });
    root.querySelector("button")?.click();
    await settle();
    expect(onCancelled).not.toHaveBeenCalled();
    expect(root.querySelector(".bp-error")?.textContent).not.toBe("");
    expect(root.querySelector("button")?.disabled).toBe(false);
  });
});
