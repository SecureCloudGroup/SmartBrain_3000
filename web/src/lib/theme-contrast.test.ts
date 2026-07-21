// WCAG contrast is a build-time law, not a review-time hope: this parses the token
// blocks in app.css and fails if any core pairing drops below its threshold — in
// EITHER theme. It exists because two real bugs shipped: white-on-accent chat bubbles
// at ~2.9:1, and a system-light block that silently omitted --warn (dark orange on
// white). The companion theme-vars test guards definedness; this guards legibility.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const appCss = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "app.css"), "utf8");

// First block = dark (:root), the [data-theme="light"] block = light. Vars are simple
// `--name: value;` lines; we only evaluate hex colors (tints/shadows use alpha and are
// decorative, not text-bearing).
function block(afterMarker: string): Record<string, string> {
  const start = appCss.indexOf(afterMarker);
  expect(start, `marker ${afterMarker} present in app.css`).toBeGreaterThan(-1);
  const body = appCss.slice(start, appCss.indexOf("}", start));
  const out: Record<string, string> = {};
  for (const m of body.matchAll(/(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})/g)) out[m[1]] = m[2];
  return out;
}

function luminance(hex: string): number {
  const c = [1, 3, 5].map((i) => {
    const v = parseInt(hex.slice(i, i + 2), 16) / 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}

function ratio(a: string, b: string): number {
  const [l1, l2] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
}

const dark = block(":root {");
const light = block(':root[data-theme="light"]');

// [foreground, background, minimum, why]
const PAIRS: Array<[string, string, number, string]> = [
  ["--text", "--bg", 4.5, "body text"],
  ["--text", "--panel", 4.5, "text on cards"],
  ["--muted", "--panel", 4.5, "secondary text carries real content"],
  ["--muted", "--bg", 4.5, "secondary text on the base surface"],
  ["--danger", "--bg", 3.0, "error text/icons (large/UI)"],
  ["--ok", "--bg", 3.0, "success chips"],
  ["--warn", "--bg", 4.5, "warn is used at meta size — hold it to text contrast"],
  ["--accent", "--bg", 3.0, "links/focus ring (UI component contrast)"],
];

describe("theme contrast (WCAG AA)", () => {
  for (const [name, vars] of [["dark", dark], ["light", light]] as const) {
    describe(name, () => {
      it("defines every color the pair list needs", () => {
        for (const [fg, bg] of PAIRS) {
          expect(vars[fg], `${fg} in ${name}`).toBeTruthy();
          expect(vars[bg], `${bg} in ${name}`).toBeTruthy();
        }
        expect(vars["--accent-strong"], `--accent-strong in ${name}`).toBeTruthy();
      });
      for (const [fg, bg, min, why] of PAIRS) {
        it(`${fg} on ${bg} >= ${min} (${why})`, () => {
          const r = ratio(vars[fg], vars[bg]);
          expect(r, `${fg} ${vars[fg]} on ${bg} ${vars[bg]} = ${r.toFixed(2)}`).toBeGreaterThanOrEqual(min);
        });
      }
      it("white on --accent-strong >= 4.5 (primary buttons)", () => {
        const r = ratio("#ffffff", vars["--accent-strong"]);
        expect(r, `#fff on ${vars["--accent-strong"]} = ${r.toFixed(2)}`).toBeGreaterThanOrEqual(4.5);
      });
    });
  }

  it("system-light media block defines the same color vars as the explicit light block", () => {
    // The regression that shipped: [data-theme=light] had --warn but the @media block didn't,
    // so system-light users got the dark orange on white.
    const media = appCss.slice(appCss.indexOf("@media (prefers-color-scheme: light)"));
    const mediaBody = media.slice(0, media.indexOf("}\n}") + 3);
    const mediaVars = new Set(Array.from(mediaBody.matchAll(/(--[a-z0-9-]+)\s*:/g), (m) => m[1]));
    const missing = Object.keys(light).filter((v) => !mediaVars.has(v));
    expect(missing, `vars in [data-theme=light] missing from the system-light media block: ${missing.join(", ")}`).toEqual([]);
  });
});
