// @vitest-environment jsdom
//
// The draft's Activate panel (Round 20, B3b; ni-format §35, ruling R6). Mounted for real, like
// CardShell.test.ts. The panel is presentational: the board page owns POST /activate (its
// request shape is pinned in api.test.ts) and hands back `busy` and the server's refusal
// sentence, so these tests drive the click and each state the page can put it in.

import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import RenderSitesPanel from "./RenderSitesPanel.svelte";

const SITES = ["cloudfront.net", "gstatic.com", "googletagmanager.com"];
const REFUSAL = "activate refused: this card is not waiting for its sites";

const mounted: Array<ReturnType<typeof mount>> = [];

afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.replaceChildren();
});

/** Mount the panel and return its root. */
function panelOf(props: { sites?: string[]; busy?: boolean; error?: string; onActivate?: () => void }): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  mounted.push(mount(RenderSitesPanel, { target, props: { sites: SITES, onActivate: () => {}, ...props } }));
  flushSync();
  const panel = target.querySelector<HTMLElement>(".sites-panel");
  expect(panel, "the panel renders").not.toBeNull();
  expect(target.querySelectorAll(".sites-panel")).toHaveLength(1);
  return panel as HTMLElement;
}

/** The panel's one Activate control. */
function activateButton(panel: HTMLElement): HTMLButtonElement {
  const buttons = panel.querySelectorAll<HTMLButtonElement>("button");
  expect(buttons, "exactly one control").toHaveLength(1);
  expect(buttons[0].closest(".sites-actions"), "the control sits under the list").not.toBeNull();
  return buttons[0];
}

describe("RenderSitesPanel — the sites a draft waits to be allowed", () => {
  it("leads with the count line, then the sites one per line as plain text, in order", () => {
    const panel = panelOf({});
    expect(panel.querySelector(".ni-note-head")?.textContent).toBe("This page also loads 3 other sites:");
    const rows = Array.from(panel.querySelectorAll(".sites-list > li"));
    expect(rows.map((li) => li.textContent)).toEqual(SITES);
    for (const li of rows) expect(li.children).toHaveLength(0);
  });

  it("says one site in the singular and names what Activate allows", () => {
    const panel = panelOf({ sites: ["cloudfront.net"] });
    expect(panel.querySelector(".ni-note-head")?.textContent).toBe("This page also loads 1 other site:");
    expect(panel.textContent).toContain("Activate is your OK for every update of this card to load exactly these sites.");
  });

  it("puts a site in as text, never as markup", () => {
    const html = '<img src="x" onerror="alert(1)">.com';
    const panel = panelOf({ sites: [html] });
    expect(panel.querySelector("img")).toBeNull();
    expect(panel.querySelector(".sites-list > li")?.textContent).toBe(html);
  });

  it("calls the page's Activate once per click", () => {
    const onActivate = vi.fn();
    const button = activateButton(panelOf({ onActivate }));
    expect(button.textContent).toBe("Activate");
    expect(button.disabled).toBe(false);
    button.click();
    expect(onActivate).toHaveBeenCalledTimes(1);
  });

  it("holds the control while the request is in flight", () => {
    const onActivate = vi.fn();
    const button = activateButton(panelOf({ busy: true, onActivate }));
    expect(button.textContent).toBe("Activating…");
    expect(button.disabled).toBe(true);
    button.click();
    expect(onActivate).not.toHaveBeenCalled();
  });

  it("shows the server's refusal sentence verbatim, and nothing when there is none", () => {
    expect(panelOf({}).querySelector(".error")).toBeNull();
    const panel = panelOf({ error: REFUSAL });
    expect(panel.querySelector(".error")?.textContent).toBe(REFUSAL);
    expect(activateButton(panel).disabled).toBe(false);
  });
});
