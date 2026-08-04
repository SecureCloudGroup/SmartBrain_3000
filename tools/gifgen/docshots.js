// The SEVEN committed guide screenshots (docs/assets/*.png + web/static/assets/*.png),
// re-shot deterministically against the same throwaway demo container as the GIF recorder.
// `./run.sh docshots` stages the two states and invokes this twice:
//   node docshots.js disconnected   -> 01-chat-connect, 02-providers, 03-local-models
//   node docshots.js connected      -> 05-knowledge, 06-remote-access, 07-update-banner,
//                                      08-always-allowed
// Output: out/docshots/*.png . Dark is canonical (operator decision) — colorScheme is pinned.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const BASE = process.env.SHOTS_BASE || "http://127.0.0.1:33096";
const OUT = path.join(__dirname, "out", "docshots");
const W = 1380, H = 900;

(async () => {
  const phase = process.argv[2] || "";
  if (phase !== "disconnected" && phase !== "connected") {
    console.error("usage: node docshots.js <disconnected|connected>");
    process.exit(1);
  }
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: { width: W, height: H },
    deviceScaleFactor: 2,
    colorScheme: "dark",
    serviceWorkers: "block", // the PWA worker would answer /api/health around page.route
  });
  const page = await ctx.newPage();
  const go = async (route) => {
    await page.goto(BASE + route, { waitUntil: "networkidle" });
    await page.waitForTimeout(600); // fonts + late fetches
  };
  const shot = async (name, opts = {}) => {
    await page.screenshot({ path: path.join(OUT, name), fullPage: true, ...opts });
    console.log("  " + name);
  };

  if (phase === "disconnected") {
    // Set up, model NOT connected, mock Ollama detectable -> the one-tap Connect card.
    await go("/chat");
    await page.waitForSelector("text=Found Ollama running", { timeout: 8000 });
    await shot("01-chat-connect.png");
    await go("/settings/providers");
    await page.waitForSelector("text=Cloud providers", { timeout: 8000 });
    await shot("02-providers.png");
    await go("/settings/models");
    await page.waitForSelector("text=Local models", { timeout: 8000 });
    await shot("03-local-models.png");
  }

  if (phase === "connected") {
    // Model connected, two documents seeded (run.sh does both).
    // 05: the Knowledge page mid-use — query typed, Best search run, a hit showing.
    await go("/knowledge");
    await page.locator('input[placeholder^="Search your knowledge"]').fill("what are my lease terms?");
    await page.locator('form button:has-text("Search")').first().click();
    await page.waitForTimeout(1200);
    await shot("05-knowledge.png");

    await go("/settings/devices");
    await page.waitForSelector("text=Remote access", { timeout: 8000 });
    await shot("06-remote-access.png");

    // 07: the in-app update banner. The backend surfaces update_ready only while a
    // launcher stamps it on every health probe, so the RECORDER plays the launcher:
    // an injected header on the page's own /api/health calls. Nothing in the app is
    // faked — the strip below is the real one users see. Cropped to the strip.
    await page.route("**/api/health", (route) => {
      const headers = { ...route.request().headers(), "x-smartbrain-update": "0.8.12" };
      route.continue({ headers });
    });
    await go("/chat");
    const banner = page.locator("text=is ready to install");
    await banner.waitFor({ timeout: 8000 });
    const box = await banner.locator("xpath=ancestor::div[1]").boundingBox();
    await shot("07-update-banner.png", {
      fullPage: false,
      clip: { x: 0, y: 0, width: W, height: Math.ceil(box.y + box.height + 16) },
    });
    await page.unroute("**/api/health"); // next headerless probe withdraws the staged update

    // 08: Always-allowed list with its Stop-allowing button — produced by the REAL flow:
    // ask for a change in Chat, then "Always allow" the proposed tool in Activity.
    await go("/chat");
    await page.locator("button:has-text(\"buy milk\")").first().click();
    await page.locator('button[aria-label="Send"]').first().click();
    await page.waitForTimeout(3000); // the mock proposes add_task; it parks for approval
    await go("/activity");
    await page.locator('button:has-text("Always allow")').first().click();
    await page.waitForSelector('summary:has-text("Always allowed")', { timeout: 8000 });
    await page.locator('summary:has-text("Always allowed")').first().click(); // expand the list
    await page.waitForSelector('button:has-text("Stop allowing")', { timeout: 8000 });
    await page.waitForTimeout(400);
    await shot("08-always-allowed.png");
  }

  await ctx.close();
  await browser.close();
})();
