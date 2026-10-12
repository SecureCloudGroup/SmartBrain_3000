// @vitest-environment jsdom
//
// The C2 panel's feed offer (Round 20, B3b; ni-format §35, ruling R12). Mounted for real, like
// CardShell.test.ts. Presentational: the board page owns POST /switch-feed (its request shape
// is pinned in api.test.ts), so these tests drive each control and each state the page can put
// the offer in.

import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { NiFeedCandidate } from "$lib/api";
import FeedOffer from "./FeedOffer.svelte";

const FEEDS: NiFeedCandidate[] = [
  { url: "https://api.gasbuddy.com/v2/stations/12345/prices?fuel=1&limit=20&format=json", site: "gasbuddy.com" },
  { url: "https://gasbuddy.com/graphql.json", site: "gasbuddy.com" },
];
const REFUSAL = "that address is not one of this card's data feeds";

const mounted: Array<ReturnType<typeof mount>> = [];

afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.replaceChildren();
});

/** Mount the offer and return its root. */
function offerOf(props: { candidates?: NiFeedCandidate[]; busy?: boolean; error?: string; onSwitch?: (url: string) => void }): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  mounted.push(mount(FeedOffer, { target, props: { candidates: FEEDS, onSwitch: () => {}, ...props } }));
  flushSync();
  const offer = target.querySelector<HTMLElement>(".feed-offer");
  expect(offer, "the offer renders").not.toBeNull();
  expect(target.querySelectorAll(".feed-offer")).toHaveLength(1);
  return offer as HTMLElement;
}

describe("FeedOffer — the page's own data feed", () => {
  it("says where the data comes from, then each exact address with one control", () => {
    const offer = offerOf({});
    expect(offer.querySelector(".ni-note-head")?.textContent).toBe("This page gets its data from a feed");
    const rows = Array.from(offer.querySelectorAll(".feed-row"));
    expect(rows).toHaveLength(2);
    rows.forEach((row, i) => {
      expect(row.querySelector(".feed-url")?.textContent).toBe(FEEDS[i].url);
      expect(row.querySelector(".feed-url")?.getAttribute("title")).toBe(FEEDS[i].url);
      const buttons = row.querySelectorAll("button");
      expect(buttons).toHaveLength(1);
      expect(buttons[0].textContent).toBe("Use this feed instead");
    });
  });

  it("keeps a long address to one line with an ellipsis, the whole address in its title", () => {
    const url = `https://api.example.com/v1/${"x".repeat(400)}.json`;
    const span = offerOf({ candidates: [{ url, site: "example.com" }] }).querySelector<HTMLElement>(".feed-url");
    expect(span?.textContent).toBe(url);
    expect(span?.getAttribute("title")).toBe(url);
    const style = getComputedStyle(span as HTMLElement);
    expect(style.whiteSpace).toBe("nowrap");
    expect(style.textOverflow).toBe("ellipsis");
    expect(style.overflow).toBe("hidden");
  });

  it("puts an address in as text, never as a link or markup", () => {
    const url = 'https://a.com/x.json?<b>bold</b><img src="y">';
    const offer = offerOf({ candidates: [{ url, site: "a.com" }] });
    expect(offer.querySelector("a, b, img")).toBeNull();
    expect(offer.querySelector(".feed-url")?.textContent).toBe(url);
  });

  it("sends the page the exact address of the feed that was tapped", () => {
    const onSwitch = vi.fn();
    const buttons = offerOf({ onSwitch }).querySelectorAll<HTMLButtonElement>(".feed-row button");
    buttons[1].click();
    expect(onSwitch).toHaveBeenCalledTimes(1);
    expect(onSwitch).toHaveBeenCalledWith(FEEDS[1].url);
    buttons[0].click();
    expect(onSwitch).toHaveBeenLastCalledWith(FEEDS[0].url);
  });

  it("holds every control while a switch is in flight", () => {
    const onSwitch = vi.fn();
    const buttons = offerOf({ busy: true, onSwitch }).querySelectorAll<HTMLButtonElement>(".feed-row button");
    for (const button of buttons) {
      expect(button.disabled).toBe(true);
      button.click();
    }
    expect(onSwitch).not.toHaveBeenCalled();
  });

  it("shows the server's refusal sentence verbatim, and nothing when there is none", () => {
    expect(offerOf({}).querySelector(".error")).toBeNull();
    expect(offerOf({ error: REFUSAL }).querySelector(".error")?.textContent).toBe(REFUSAL);
  });
});
