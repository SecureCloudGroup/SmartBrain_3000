// @vitest-environment jsdom
//
// IrPaint mounted for real with a CLIR that arrives the way the board hands it over: from a
// deep `$state`, i.e. as nested Svelte proxies. Until 2026-10-14 the painter cloned it with
// structuredClone, which throws on any Proxy, so every designed card left the board on its
// spinner while every unit test (plain fixture objects) stayed green.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it } from "vitest";

import IrPaint from "./IrPaint.svelte";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIX = join(HERE, "..", "..", "..", "..", "app", "tests", "fixtures", "ni_forms", "clir");

function loadFixture(name: string): unknown {
  return JSON.parse(readFileSync(join(FIX, `${name}.clir.json`), "utf8"));
}

/** A deep Proxy over plain data — what a Svelte deep `$state` hands a component. */
function deepProxy<T>(v: T): T {
  if (v === null || typeof v !== "object") return v;
  if (Array.isArray(v)) return new Proxy(v.map(deepProxy), {}) as T;
  return new Proxy(Object.fromEntries(Object.entries(v).map(([k, x]) => [k, deepProxy(x)])), {}) as T;
}

// jsdom has no ResizeObserver; IrPaint observes its plane's width (a no-op stub is enough here).
if (!("ResizeObserver" in globalThis)) {
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  };
}

const mounted: Array<ReturnType<typeof mount>> = [];

afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.replaceChildren();
});

function paint(clir: unknown): HTMLElement {
  const target = document.createElement("div");
  document.body.appendChild(target);
  mounted.push(mount(IrPaint, { target, props: { clir, viewerTz: "UTC" } }));
  flushSync();
  return target;
}

describe("IrPaint with a CLIR from reactive state", () => {
  it("paints a stat card handed over as nested proxies", () => {
    const errors: unknown[] = [];
    const onError = (e: ErrorEvent) => errors.push(e.error ?? e.message);
    window.addEventListener("error", onError);
    const root = paint(deepProxy(loadFixture("stat_nvda")));
    window.removeEventListener("error", onError);
    expect(errors).toEqual([]);
    expect(root.textContent).toContain("NVDA");
    expect(root.textContent).not.toMatch(/unrenderable|can't be shown|cannot be shown/i);
  });

  it("paints a live-bound card (tide curve) handed over as nested proxies", () => {
    const root = paint(deepProxy(loadFixture("curve_tide")));
    expect(root.querySelectorAll("svg").length).toBeGreaterThan(0);
    expect(root.textContent).not.toMatch(/unrenderable|can't be shown|cannot be shown/i);
  });
});
