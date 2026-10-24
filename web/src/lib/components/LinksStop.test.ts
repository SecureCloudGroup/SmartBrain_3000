// @vitest-environment jsdom
//
// LinksStop — the v2 honest stop: read-only links plus paste-a-link, no source to pick.

import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { NiSuggestion } from "$lib/api";
import LinksStop from "./LinksStop.svelte";

const LINKS: NiSuggestion[] = [
  { kind: "link", found: "dataset", title: "Crimes", host: "data.cityofchicago.org", url: "https://data.cityofchicago.org/crimes" },
  { kind: "link", found: "page", title: "Bad", host: "x.com", url: "http://x.com/insecure" },
  { kind: "library", title: "A source", host: "api.example.com", url: "https://api.example.com/v1" },
];

const mounted: Array<ReturnType<typeof mount>> = [];
afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.replaceChildren();
});

function stopOf(props: {
  suggestions?: NiSuggestion[];
  error?: string;
  onPaste?: (url: string, needsKey: boolean) => Promise<boolean>;
}): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  mounted.push(mount(LinksStop, { target, props: { onPaste: () => Promise.resolve(true), ...props } }));
  flushSync();
  const root = target.querySelector<HTMLElement>(".ls");
  expect(root, "the stop card renders").not.toBeNull();
  return root as HTMLElement;
}

const settle = async () => { await Promise.resolve(); await Promise.resolve(); flushSync(); };

describe("LinksStop", () => {
  it("lists only https link rows, read-only, and offers no source to tap", () => {
    const root = stopOf({ suggestions: LINKS });
    const anchors = Array.from(root.querySelectorAll("a"));
    expect(anchors).toHaveLength(1);
    expect(anchors[0].getAttribute("href")).toBe("https://data.cityofchicago.org/crimes");
    expect(anchors[0].getAttribute("target")).toBe("_blank");
    expect(root.textContent).not.toContain("A source");
    expect(root.querySelectorAll("button")).toHaveLength(1);
    expect(root.querySelector("button")?.textContent).toBe("Use this URL");
  });

  it("with no links it still says so and keeps the paste field", () => {
    const root = stopOf({});
    expect(root.querySelector(".ls-head")?.textContent).toBe("No SmartBrain Library source answers this yet.");
    expect(root.querySelector("ul")).toBeNull();
    expect(root.querySelector("input")?.getAttribute("placeholder")).toContain("Or paste any web page");
  });

  it("paste sends the trimmed link and clears the field only when accepted", async () => {
    const onPaste = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const root = stopOf({ onPaste });
    const input = root.querySelector("input") as HTMLInputElement;
    const submit = root.querySelector("button") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    input.value = "  https://data.example.gov/feed.json ";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    flushSync();
    expect(submit.disabled).toBe(false);
    root.querySelector("form")?.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    await settle();
    expect(onPaste).toHaveBeenLastCalledWith("https://data.example.gov/feed.json", false);
    expect(input.value).not.toBe("");
    root.querySelector("form")?.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    await settle();
    expect(onPaste).toHaveBeenCalledTimes(2);
    expect(input.value).toBe("");
  });

  it("says the link needs the user's key only when ticked, and only for an https link", async () => {
    const onPaste = vi.fn().mockResolvedValue(true);
    const root = stopOf({ onPaste });
    const [input, box] = Array.from(root.querySelectorAll("input")) as HTMLInputElement[];
    expect(box.type).toBe("checkbox");
    box.click();
    input.value = "http://data.example.gov/feed.json";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    flushSync();
    root.querySelector("form")?.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    await settle();
    expect(onPaste).toHaveBeenLastCalledWith("http://data.example.gov/feed.json", false);
    box.click();
    input.value = "https://api.rates.example.org/latest";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    flushSync();
    root.querySelector("form")?.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    await settle();
    expect(onPaste).toHaveBeenLastCalledWith("https://api.rates.example.org/latest", true);
    expect(box.checked).toBe(false);   // an accepted paste clears the tick too
  });

  it("shows the server's refusal", () => {
    const root = stopOf({ error: "that address is not allowed" });
    expect(root.querySelector(".error")?.textContent).toBe("that address is not allowed");
    expect(root.querySelectorAll(".error")).toHaveLength(1);
  });
});
