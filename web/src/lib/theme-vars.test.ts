// Guard against the bug class that shipped twice: a CSS custom property used in a
// component but never defined in the theme (app.css) silently falls back to its hardcoded
// default — which was a DARK panel color, so dialogs/bars rendered unreadable in light mode
// (the Confirm dialog, the email dialog, the Knowledge action bar). svelte-check can't catch
// this. This test fails if any var(--x) used anywhere in src/ is not defined in app.css.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC = join(dirname(fileURLToPath(import.meta.url)), ".."); // web/src

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(svelte|css|ts)$/.test(name) && !name.endsWith(".test.ts")) out.push(p);
  }
  return out;
}

describe("theme CSS variables", () => {
  it("every var(--x) used in src/ is defined in app.css", () => {
    const appCss = readFileSync(join(SRC, "app.css"), "utf8");
    const defined = new Set(Array.from(appCss.matchAll(/^\s*(--[a-z-]+)\s*:/gm), (m) => m[1]));
    expect(defined.size).toBeGreaterThan(5); // sanity: we actually parsed the theme

    const used = new Set<string>();
    for (const file of walk(SRC)) {
      const text = readFileSync(file, "utf8");
      for (const m of text.matchAll(/var\((--[a-z-]+)/g)) used.add(m[1]);
    }

    const undefinedVars = [...used].filter((v) => !defined.has(v)).sort();
    expect(undefinedVars, `CSS vars used but not defined in app.css: ${undefinedVars.join(", ")}`).toEqual([]);
  });
});
