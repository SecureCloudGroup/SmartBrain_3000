// Guard against the bug class that shipped twice: a CSS custom property used in a
// component but never defined in the theme (app.css) silently falls back to its hardcoded
// default — which was a DARK panel color, so dialogs/bars rendered unreadable in light mode
// (the Confirm dialog, the email dialog, the Knowledge action bar). svelte-check can't catch
// this. This test fails if any var(--x) used anywhere in src/ is not defined in app.css.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { NI_CSS_DARK, NI_CSS_LIGHT, NI_TOKEN_NAMES } from "./ni/tokens";

const SRC = join(dirname(fileURLToPath(import.meta.url)), ".."); // web/src

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(svelte|css|ts)$/.test(name) && !name.endsWith(".test.ts")) out.push(p);
  }
  return out;
}

const FAMILIES: Record<string, string[]> = { "--ni-": NI_TOKEN_NAMES.map((n) => `--ni-${n}`) };

describe("theme CSS variables", () => {
  it("every var(--x) used in src/ is defined in app.css", () => {
    const appCss = readFileSync(join(SRC, "app.css"), "utf8");
    const defined = new Set(Array.from(appCss.matchAll(/^\s*(--[a-z0-9-]+)\s*:/gm), (m) => m[1]));
    expect(defined.size).toBeGreaterThan(5); // sanity: we actually parsed the theme

    const used = new Set<string>();
    for (const file of walk(SRC)) {
      const text = readFileSync(file, "utf8");
      // A var() carrying an explicit fallback — var(--x, 0) — is self-sufficient by
      // construction (dynamic per-element vars like the mic level ring set it inline);
      // the guard is for THEME tokens that must exist in app.css.
      for (const m of text.matchAll(/var\((--[a-z0-9-]+)(,)?/g)) {
        if (m[2]) continue;
        // The NI painter writes `var(--ni-${token})` with the token NAME decided at
        // runtime, so the scanner sees the family prefix. Expand it to every generated
        // token: each one must be defined, in both themes, like any other theme var.
        const family = FAMILIES[m[1]];
        if (family) family.forEach((v) => used.add(v));
        else used.add(m[1]);
      }
    }

    const undefinedVars = [...used].filter((v) => !defined.has(v)).sort();
    expect(undefinedVars, `CSS vars used but not defined in app.css: ${undefinedVars.join(", ")}`).toEqual([]);
  });

  it("the --ni-* regions in app.css are what gen-ni-tokens.mjs generates", () => {
    // tokens.ts and the three app.css regions come from one generator run; a hand edit
    // or a regenerate that skipped app.css shows up here (the painter would otherwise
    // paint colours the Python painter does not).
    const appCss = readFileSync(join(SRC, "app.css"), "utf8");
    const lines = (block: string) => block.split("\n").map((l) => l.trim()).filter(Boolean);
    const regions: Record<string, string[]> = {};
    const re = /\/\* ni-tokens:([a-z-]+):start[\s\S]*?\*\/\n([\s\S]*?)\n\s*\/\* ni-tokens:\1:end \*\//g;
    for (const m of appCss.matchAll(re)) regions[m[1]] = lines(m[2]);
    expect(Object.keys(regions).sort()).toEqual(["dark", "light", "light-system"]);
    expect(regions.dark).toEqual(lines(NI_CSS_DARK));
    expect(regions.light).toEqual(lines(NI_CSS_LIGHT));
    expect(regions["light-system"]).toEqual(lines(NI_CSS_LIGHT));
  });
});
