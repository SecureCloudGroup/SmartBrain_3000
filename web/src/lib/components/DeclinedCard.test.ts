// @vitest-environment jsdom
//
// DeclinedCard — the honest decline: the headline, the one reason line, up to three read-only links, and the
// ways back the page declares. It builds nothing and fetches nothing.

import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FlowDecline } from "$lib/ni/types.gen";
import DeclinedCard from "./DeclinedCard.svelte";

const DECLINE: FlowDecline = {
  reason: "no_answering_source",
  headline: "I couldn't build a reliable card for this",
  line: "No source I can read answers this.",
  links: [
    { title: "City open data", url: "https://data.example.gov/crimes", host: "data.example.gov" },
    { title: "Insecure", url: "http://x.example/a", host: "x.example" },
  ],
};

const mounted: Array<ReturnType<typeof mount>> = [];
afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.replaceChildren();
});

function cardOf(props: Record<string, unknown> = {}): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  const noop = () => {};
  mounted.push(mount(DeclinedCard, {
    target,
    props: { decline: DECLINE, onFix: noop, onRefine: noop, onRetry: noop, ...props },
  }));
  flushSync();
  const root = target.querySelector<HTMLElement>(".dc");
  expect(root, "the declined card renders").not.toBeNull();
  return root as HTMLElement;
}

const labels = (root: HTMLElement) => Array.from(root.querySelectorAll("button")).map((b) => b.textContent?.trim());

describe("DeclinedCard", () => {
  it("says it could not build a reliable card, gives the reason and the https links only", () => {
    const root = cardOf();
    expect(root.querySelector(".dc-head")?.textContent).toBe("I couldn't build a reliable card for this");
    expect(root.querySelector(".dc-line")?.textContent).toBe("No source I can read answers this.");
    expect(root.getAttribute("data-reason")).toBe("no_answering_source");
    const anchors = Array.from(root.querySelectorAll("a"));
    expect(anchors.map((a) => a.getAttribute("href"))).toEqual(["https://data.example.gov/crimes"]);
    expect(anchors[0].getAttribute("target")).toBe("_blank");
    expect(anchors[0].getAttribute("rel")).toContain("noopener");
  });

  it("with no links it still states the decline and offers no list", () => {
    const root = cardOf({ decline: { ...DECLINE, links: [] } });
    expect(root.querySelector("ul")).toBeNull();
    expect(root.querySelector(".dc-line")?.textContent).toBe(DECLINE.line);
  });

  it("offers only the reopen actions the page declares", () => {
    expect(labels(cardOf())).toEqual([]);
    expect(labels(cardOf({ canRetry: true }))).toEqual(["Try again"]);
    expect(labels(cardOf({ canFix: true, canRefine: true }))).toEqual(["Fix", "Refine…"]);
  });

  it("each reopen button calls its own handler and is disabled while busy", () => {
    const onFix = vi.fn();
    const onRetry = vi.fn();
    const root = cardOf({ canFix: true, canRetry: true, onFix, onRetry });
    const [fix, retry] = Array.from(root.querySelectorAll("button"));
    fix.click();
    retry.click();
    expect(onFix).toHaveBeenCalledTimes(1);
    expect(onRetry).toHaveBeenCalledTimes(1);
    const busy = cardOf({ canFix: true, canRetry: true, busy: true });
    expect(Array.from(busy.querySelectorAll("button")).every((b) => b.disabled)).toBe(true);
  });

  it("shows the server's refusal of a reopen", () => {
    const root = cardOf({ error: "nothing to reopen" });
    expect(root.querySelector(".error")?.textContent).toBe("nothing to reopen");
  });
});
