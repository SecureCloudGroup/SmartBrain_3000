// @vitest-environment jsdom
//
// The brief card's opt-in summary (plan S8). Mounted for real; `$lib/api` is mocked. The headlines are the
// card's own form; this block is the only place a model's words can appear, always under the server's label.

import { flushSync, mount, unmount } from "svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { NiBoardItem } from "$lib/api";

// The real api.ts imports $app/navigation, which jsdom's import analysis cannot resolve: the module is replaced here.
const mocks = vi.hoisted(() => {
  class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  }
  return { ApiError, niBriefSummary: vi.fn(), niRun: vi.fn() };
});
vi.mock("$lib/api", () => ({ ApiError: mocks.ApiError, api: { niBriefSummary: mocks.niBriefSummary, niRun: mocks.niRun } }));

import BriefSummary from "./BriefSummary.svelte";

const LABEL = "Summary written by your local model";
const mounted: Array<ReturnType<typeof mount>> = [];

const { niBriefSummary, niRun, ApiError } = mocks;

beforeEach(() => {
  niBriefSummary.mockReset().mockResolvedValue({ ok: true });
  niRun.mockReset().mockResolvedValue({});
});
afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.replaceChildren();
});

function item(brief: Record<string, unknown> | undefined, over: Record<string, unknown> = {}): NiBoardItem {
  return { id: "c1", state: "live", awaiting_yes: false, brief, ...over } as unknown as NiBoardItem;
}

function render(it: NiBoardItem, onchanged = vi.fn()): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  mounted.push(mount(BriefSummary, { target, props: { item: it, onchanged } }));
  flushSync();
  return target;
}

const OFF = { summary: false, local: true, state: "off", label: LABEL, text: "" };

describe("BriefSummary", () => {
  it("renders nothing for a card that is not a brief", () => {
    expect(render(item(undefined)).querySelector(".brief")).toBeNull();
  });

  it("off: the switch is unchecked, the copy says headlines only and nothing goes to a cloud model", () => {
    const root = render(item(OFF));
    const input = root.querySelector<HTMLInputElement>("input[role=switch]")!;
    expect(input.checked).toBe(false);
    expect(input.disabled).toBe(false);
    expect(root.querySelector(".brief-help")?.textContent).toContain("Nothing is sent to a cloud model");
    expect(root.querySelector(".brief-label")).toBeNull();
  });

  it("on and ready: the text sits under the label the server sent", () => {
    const root = render(item({ ...OFF, summary: true, state: "ready", text: "Three stories today." }));
    expect(root.querySelector(".brief-label")?.textContent).toBe(LABEL);
    expect(root.querySelector(".brief-text")?.textContent).toBe("Three stories today.");
    expect(root.querySelector<HTMLInputElement>("input[role=switch]")!.checked).toBe(true);
  });

  it("on with no text yet says the next refresh writes it; unavailable says the headlines are current", () => {
    expect(render(item({ ...OFF, summary: true, state: "pending" })).querySelector(".brief-note")?.textContent).toContain("next refresh");
    expect(render(item({ ...OFF, summary: true, state: "unavailable" })).textContent).toContain("headlines above are current");
  });

  it("no local model: the switch is disabled and the copy says so honestly", () => {
    const root = render(item({ ...OFF, local: false }));
    expect(root.querySelector<HTMLInputElement>("input[role=switch]")!.disabled).toBe(true);
    expect(root.querySelector(".brief-help")?.textContent).toContain("No local model");
  });

  it("a held card (waiting for Keep) shows no switch", () => {
    expect(render(item(OFF, { awaiting_yes: true })).querySelector(".brief")).toBeNull();
  });

  it("turning it on asks the server, refreshes once, then tells the board", async () => {
    const onchanged = vi.fn();
    const root = render(item(OFF), onchanged);
    root.querySelector<HTMLInputElement>("input[role=switch]")!.click();
    await vi.waitFor(() => expect(onchanged).toHaveBeenCalled());
    expect(niBriefSummary).toHaveBeenCalledWith("c1", true);
    expect(niRun).toHaveBeenCalledWith("c1");
  });

  it("turning it off does not refresh", async () => {
    const onchanged = vi.fn();
    const root = render(item({ ...OFF, summary: true, state: "ready", text: "x" }), onchanged);
    root.querySelector<HTMLInputElement>("input[role=switch]")!.click();
    await vi.waitFor(() => expect(onchanged).toHaveBeenCalled());
    expect(niBriefSummary).toHaveBeenCalledWith("c1", false);
    expect(niRun).not.toHaveBeenCalled();
  });

  it("a refusal from the server is shown as a sentence and the board is not told", async () => {
    niBriefSummary.mockRejectedValue(new ApiError(409, "no local model is set up"));
    const onchanged = vi.fn();
    const root = render(item(OFF), onchanged);
    root.querySelector<HTMLInputElement>("input[role=switch]")!.click();
    await vi.waitFor(() => expect(root.querySelector("[role=alert]")).not.toBeNull());
    expect(root.querySelector("[role=alert]")?.textContent).toContain("no local model");
    expect(onchanged).not.toHaveBeenCalled();
  });
});
