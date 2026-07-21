// Deterministic screenshot grid of every route — the before/after evidence for design-stage
// PRs. Runs against the same throwaway demo container as the GIF recorder (./run.sh shots
// starts it, seeds content, then invokes this). Output: out/shots/<viewport>-<theme>-<route>.png
// (gitignored — the grid is pasted into PRs, never committed). Requires: playwright.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const BASE = process.env.SHOTS_BASE || "http://127.0.0.1:33096";
const OUT = path.join(__dirname, "out", "shots");
const ROUTES = [
  "/chat", "/knowledge", "/planner", "/schedules", "/email", "/activity",
  "/usage", "/settings", "/settings/models", "/settings/router", "/settings/web", "/help",
];
const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 390, height: 844 },
];
const THEMES = ["dark", "light"]; // the app honors prefers-color-scheme when no explicit choice is saved

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  let count = 0;
  for (const vp of VIEWPORTS) {
    for (const theme of THEMES) {
      const ctx = await browser.newContext({
        viewport: { width: vp.width, height: vp.height },
        colorScheme: theme,
        deviceScaleFactor: 2,
      });
      const page = await ctx.newPage();
      for (const route of ROUTES) {
        const slug = route.slice(1).replace(/\//g, "-");
        await page.goto(BASE + route, { waitUntil: "networkidle" }).catch(() => {});
        await page.waitForTimeout(500); // let fonts and late fetches settle
        await page.screenshot({ path: path.join(OUT, `${vp.name}-${theme}-${slug}.png`), fullPage: true });
        count += 1;
        process.stdout.write(`  ${vp.name}-${theme}-${slug}.png\n`);
      }
      await ctx.close();
    }
  }
  await browser.close();
  console.log(`${count} shots -> ${OUT}`);
})();
