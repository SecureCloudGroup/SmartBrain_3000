// @vitest-environment jsdom
//
// TriedList — what the build tried and why each source was dropped, in the backend's words. Nothing when nothing
// was tried; at most six rows.

import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it } from "vitest";

import TriedList from "./TriedList.svelte";

const mounted: Array<ReturnType<typeof mount>> = [];
afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.replaceChildren();
});

function listOf(tried: unknown): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  mounted.push(mount(TriedList, { target, props: { tried: tried as never } }));
  flushSync();
  return target;
}

const texts = (root: HTMLElement) =>
  Array.from(root.querySelectorAll("li")).map((li) => li.textContent?.replace(/\s+/g, " ").trim());

describe("TriedList", () => {
  it("names each dropped source with its reason, verbatim, the provider first and the host when there is none", () => {
    const root = listOf([
      { host: "www.alphavantage.co", provider: "Alpha Vantage",
        why: "answered with a message instead of data: Thank you for using Alpha Vantage!" },
      { host: "finnhub.io", provider: "", why: "was skipped: you have no key for it" },
    ]);
    expect(root.querySelector(".tried-head")?.textContent).toBe("What I tried");
    expect(texts(root)).toEqual([
      "Alpha Vantage answered with a message instead of data: Thank you for using Alpha Vantage!",
      "finnhub.io was skipped: you have no key for it",
    ]);
  });

  it("renders nothing when nothing was tried, and at most six rows", () => {
    expect(listOf([]).querySelector(".tried")).toBeNull();
    expect(listOf(undefined).querySelector(".tried")).toBeNull();
    const many = Array.from({ length: 8 }, (_, i) => ({ host: `h${i}.example`, provider: "", why: "x" }));
    expect(listOf(many).querySelectorAll("li")).toHaveLength(6);
  });
});
