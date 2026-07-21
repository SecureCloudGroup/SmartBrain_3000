// Record one quickstart clip:  node clips.js <NN>   (NN = 01..10).  See README.md
// for the demo-container + per-clip state each one needs. Output: ./video/*.webm.
const { chromium, Rec } = require("./lib");
const URL = process.env.DEMO_URL || "http://localhost:33096";
const W = 1280, H = 800;
const PASS = process.env.DEMO_PASS || "correct-horse-battery";
const RK = process.env.RECOVERY_KEY || "";
const MSG = 'textarea[placeholder^="Message"]';
const NEW = 'input[placeholder="New task…"]';
const ADD = 'form button:has-text("Add")';

async function open(opts = {}) {
  const browser = await chromium.launch();
  // colorScheme dark: the docs/videos are DARK canonical (operator decision) — without
  // this, Playwright's default prefers-color-scheme is LIGHT and the app follows it.
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 2, colorScheme: "dark", recordVideo: { dir: "video", size: { width: W, height: H } }, ...opts });
  const page = await ctx.newPage();
  await page.goto(URL, { waitUntil: "networkidle" });
  const R = new Rec(page); await R.init();
  return { browser, ctx, page, R };
}
const done = async (h) => { await h.ctx.close(); await h.browser.close(); };
const nav = (page, name) => page.getByRole("link", { name, exact: true }).first();

const CLIPS = {
  // 01 install -> unlocked (FRESH demo, NOT set up). Hero clip.
  "01": async () => {
    const h = await open(); const { page, R } = h;
    await R.card(`<div style="font-size:48px;font-weight:800;margin-bottom:16px">SmartBrain_3000</div><div style="font-size:30px;color:#9aa7b4">From one command to your unlocked Chat</div>`);
    await R.cap("Set up SmartBrain and reach Chat", "1/4"); await R.dwell(1700);
    await R.card(`<div style="font-family:ui-monospace,Menlo,monospace;text-align:left;background:#0d1117;border:1px solid #30363d;border-radius:12px;padding:24px 28px;font-size:21px;line-height:1.65;color:#c9d1d9;min-width:700px;box-shadow:0 12px 40px rgba(0,0,0,.5)"><div style="color:#7ee787">$ <span style="color:#e6edf3">python3 installer/install.py install</span></div><div style="color:#8b949e">..   Building the image and starting the stack…</div><div style="color:#8b949e">..   First run builds the image — a few minutes</div><div style="color:#7ee787">OK&nbsp;&nbsp; Running at http://localhost:33000</div></div>`);
    await R.cap("One command installs everything", "1/4"); await R.dwell(2500);
    await R.card("", false);
    await R.cap("Choose a passphrase — it encrypts everything", "2/4");
    await R.highlight("#pp"); await R.type("#pp", PASS); await R.dwell(650);
    await R.type("#cf", PASS); await R.highlight("#cf"); await R.dwell(850); await R.noring();
    await R.click('button:has-text("Create vault")');
    await page.waitForSelector("text=Save your Emergency Kit", { timeout: 8000 }); await R.ensure(); await R.dwell(500);
    await page.evaluate(() => { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); const re = /([A-Z2-7]{4}-){4,}[A-Z2-7]{2,4}/; let n; while ((n = w.nextNode())) if (re.test(n.nodeValue)) n.nodeValue = n.nodeValue.replace(re, "DEMO-XXXX-XXXX-XXXX-XXXX-XXXX"); });
    await R.cap("Save this now — there is NO password reset", "3/4"); await R.dwell(1300);
    await R.scrollCenter('button:has-text("Continue")'); await R.dwell(300);
    await R.click('button:has-text("Download (.txt)")');
    await R.cap("Store it offline — the only way back in", "3/4"); await R.dwell(800);
    await R.moveTo('label:has-text("saved")');
    await page.locator("input[type=checkbox]").first().check({ force: true }); await R.dwell(700); await R.noring();
    await R.click('button:has-text("Continue")');
    await page.waitForTimeout(1300); await R.ensure();
    await R.cap("Unlocked. You’re in.", "4/4"); await R.dwell(2000); await done(h);
  },
  // 02 connect a model (mock /reset first; demo SET UP, NOT connected).
  "02": async () => {
    const h = await open(); const { page, R } = h;
    await R.cap("Give your assistant a brain", "1/3"); await R.dwell(1600);
    await R.cap("Running Ollama? One tap connects it — and stays on your machine", "1/3");
    await R.click('button:has-text("Connect")'); await page.waitForTimeout(1800); await R.ensure();
    await R.cap("Local model connected — free, fully on-box", "2/3"); await R.dwell(2000);
    await nav(page, "Settings").click(); await page.waitForTimeout(800);
    await page.getByRole("tab", { name: "Cloud providers" }).first().click().catch(() => {}); await page.waitForTimeout(700); await R.ensure();
    await R.cap("No Ollama? Bring your own cloud key", "3/3");
    await R.moveTo("#k-anthropic"); await R.type("#k-anthropic", "sk-ant-DEMO-0000000000000000"); await R.dwell(600);
    await R.click('xpath=//input[@id="k-anthropic"]/following::button[contains(.,"Save")][1]'); await page.waitForTimeout(1000); await R.ensure();
    await R.cap("Keys are stored encrypted — never sent to us", "3/3"); await R.dwell(2200); await done(h);
  },
  // 03 first chat (demo SET UP + model connected).
  "03": async () => {
    const h = await open(); const { page, R } = h;
    await R.cap("Send your first message", "1/3"); await R.dwell(1600);
    await R.cap("Tap a suggestion — or type your own", "1/3");
    await R.click('button:has-text("What can you do?")'); await R.dwell(500);
    await R.cap("Press Send", "1/3"); await R.click('button[aria-label="Send"]');
    await R.cap("It answers using your connected model", "2/3"); await page.waitForTimeout(3800); await R.ensure();
    await R.cap("A real reply — your model is working", "2/3"); await R.dwell(2400);
    await R.cap("It reads freely — anything that changes data waits for approval", "3/3"); await R.dwell(2200); await done(h);
  },
  // 04 knowledge (demo SET UP + model connected). A FILE upload (not a note): only a file/URL
  // carries a source, and the source is what the citation chips cite. Uploads auto-index —
  // there is no Reindex beat any more.
  "04": async () => {
    const h = await open(); const { page, R } = h;
    const LEASE = [
      "RENTAL AGREEMENT — 414 Maple Court, Unit 3B",
      "",
      "This agreement is made between Pat Rivera (landlord) and the tenant.",
      "The unit is rented unfurnished, with parking spot #12 included.",
      "",
      "Lease term: 12 months. Rent: $1,800 per month, due on the 1st.",
      "Notice to vacate: 60 days, in writing. A late fee applies after the 5th.",
      "",
      "Utilities: tenant pays electric and internet; water and trash included.",
      "Security deposit: one month's rent, returned within 21 days of move-out.",
    ].join("\n");
    await nav(page, "Knowledge").click(); await page.waitForTimeout(800); await R.ensure();
    await R.cap("Add private knowledge — encrypted on your machine", "1/4"); await R.dwell(1500);
    await R.moveTo(".drop"); await R.highlight(".drop");
    await page.setInputFiles('input[type="file"][multiple]', { name: "Apartment-Lease.txt", mimeType: "text/plain", buffer: Buffer.from(LEASE) });
    await page.waitForTimeout(1200); await R.noring(); await R.ensure();
    await R.cap("Drop a file in — it's indexed automatically", "1/4"); await R.dwell(1700);
    await R.cap("Search by meaning, not exact words", "2/4");
    await R.type('input[placeholder^="Search your knowledge"]', "what are my lease terms?"); await R.dwell(400);
    await R.click('form button:has-text("Search")'); await page.waitForTimeout(1200); await R.ensure();
    await R.highlight(".hit .chip");
    await R.cap("Every result is a citation — the file it came from", "2/4"); await R.dwell(1900); await R.noring();
    await R.click(".hit .chip"); await page.waitForTimeout(900); await R.ensure();
    await R.cap("Click it — the document opens at the matching passage", "3/4"); await R.dwell(2200);
    // The viewer is a true Modal now — close it before navigating (its overlay blocks the nav).
    await page.keyboard.press("Escape"); await page.waitForTimeout(400);
    await nav(page, "Chat").click(); await page.waitForTimeout(800); await R.ensure();
    await R.cap("Or just ask Chat — it reads your knowledge for you", "4/4");
    await R.type(MSG, "What does my knowledge say about my lease?"); await R.dwell(400);
    await R.click('button[aria-label="Send"]'); await page.waitForTimeout(4500); await R.ensure();
    if (await page.locator(".cites .chip").count()) {
      await R.highlight(".cites");
      await R.cap("The answer cites its sources — a chip opens the passage", "4/4");
    } else {
      await R.cap("Answered from your knowledge — no approval needed to read", "4/4");
    }
    await R.dwell(2400); await done(h);
  },
  // 05 approval loop (demo SET UP + model connected; empty Activity/Planner).
  "05": async () => {
    const h = await open(); const { page, R } = h;
    await R.cap("Anything that changes data waits for your OK", "1/4"); await R.dwell(1700);
    await R.cap("Ask it to change something", "1/4");
    await R.click("button:has-text(\"buy milk\")"); await R.dwell(400);
    await R.click('button[aria-label="Send"]'); await page.waitForTimeout(2800); await R.ensure();
    await R.cap("It proposes the action — it won’t run it on its own", "2/4"); await R.dwell(2300);
    await R.click("a:has-text('Activity')"); await page.waitForTimeout(1100); await R.ensure();
    await R.cap("Open Activity to review what it wants to do", "3/4"); await R.dwell(1800);
    await R.click('button:has-text("Approve")'); await page.waitForTimeout(1300); await R.ensure();
    await R.cap("Approve — every attempt is logged in the audit", "3/4"); await R.dwell(1600);
    await nav(page, "Planner").click(); await page.waitForTimeout(1100); await R.ensure();
    await R.cap("Approved — and now it’s done", "4/4"); await R.dwell(2200); await done(h);
  },
  // 06 planner (demo SET UP; pre-seed a Today task + a This-week task via API).
  "06": async () => {
    const h = await open(); const { page, R } = h;
    const showGroup = async (name) => { await page.locator(`text=${name}`).first().scrollIntoViewIfNeeded().catch(() => {}); await page.evaluate(() => window.scrollBy(0, -90)); };
    await nav(page, "Planner").click(); await page.waitForTimeout(900); await R.ensure();
    await showGroup("Today"); await R.cap("Your tasks, grouped by when they’re due", "1/3"); await R.dwell(2000);
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
    await R.cap("Add a task — a due date is optional", "2/3");
    await R.moveTo(NEW); await R.type(NEW, "Buy birthday gift"); await R.dwell(550); await R.noring();
    await R.click(ADD); await page.waitForTimeout(1100);
    await showGroup("No date"); await R.ensure();
    await R.highlight("text=Buy birthday gift").catch(() => {});
    await R.cap("No due date? It drops to the bottom, under “No date”", "3/3"); await R.dwell(2000); await R.noring();
    await R.cap("Grouped by due date — the assistant can propose tasks too", "3/3"); await R.dwell(1800); await done(h);
  },
  // 07 schedule (demo SET UP + model connected; pre-seed a couple tasks via API).
  "07": async () => {
    const h = await open(); const { page, R } = h;
    await nav(page, "Schedules").click(); await page.waitForTimeout(800); await R.ensure();
    await R.cap("Optional: run a prompt on a timer", "1/3"); await R.dwell(1500);
    // Schedules opens on the Output tab now; the Create form lives under the Create subtab.
    await R.click('button[role="tab"]:has-text("Create")'); await page.waitForTimeout(500); await R.ensure();
    await R.type('input[placeholder^="Name"]', "Morning task summary");
    await R.type('textarea[placeholder^="What should it do"]', "Summarize my open tasks"); await R.dwell(400);
    await page.locator("select").nth(0).selectOption({ label: "Daily" }).catch(() => {});
    await R.cap("Name it, give it a prompt, set a cadence", "2/3"); await R.dwell(800);
    await R.click('button:has-text("Add schedule")'); await page.waitForTimeout(1100); await R.ensure();
    await R.cap("Fire it now to see it work", "3/3"); await R.click('button:has-text("Run now")'); await page.waitForTimeout(3500); await R.ensure();
    await R.cap("A real summary of your open tasks — dangerous actions still ask first", "3/3"); await R.dwell(2600); await done(h);
  },
  // 08 pair a phone (demo SET UP; WEBRTC off is fine — pairing provisions a device).
  "08": async () => {
    const h = await open(); const { page, R } = h;
    await nav(page, "Settings").click(); await page.waitForTimeout(600);
    await page.getByRole("tab", { name: "Remote access" }).first().click(); await page.waitForTimeout(700); await R.ensure();
    await R.cap("Optional: reach SmartBrain from your phone — off by default", "1/3"); await R.dwell(1700);
    await R.cap("Name your phone, then pair it", "1/3");
    await R.moveTo('input[placeholder="My phone"]');
    await page.locator('input[placeholder="My phone"]').first().fill("My iPhone"); await R.dwell(500); await R.noring();
    await R.click('button:has-text("Pair a new phone")');
    await R.cap("On your phone: scan the QR, install the app, enter the code", "2/3"); await page.waitForTimeout(2500); await R.ensure();
    await page.evaluate(() => { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); const re = /^[A-Z0-9]{6}$/; let n; while ((n = w.nextNode())) if (re.test(n.nodeValue.trim())) { n.nodeValue = "DEMO42"; break; } window.scrollTo(0, document.body.scrollHeight); });
    await page.evaluate(() => { document.getElementById("__cap").style.opacity = "0"; }); // unobstructed reveal
    await R.dwell(3000);
    await page.evaluate(() => { document.getElementById("__cap").style.opacity = "1"; });
    await R.cap("End-to-end encrypted over WebRTC — no router setup", "3/3"); await R.dwell(2400); await done(h);
  },
  // 09 backup/recovery (demo SET UP; capture RECOVERY_KEY from /api/account/setup).
  "09": async () => {
    const h = await open({ acceptDownloads: true }); const { page, R } = h;
    await nav(page, "Settings").click(); await page.waitForTimeout(700);
    await page.getByRole("tab", { name: "Account & Data" }).first().click(); await page.waitForTimeout(700); await R.ensure();
    await R.cap("Optional: back up, and prove your way back in", "1/4"); await R.dwell(1600);
    await R.cap("A complete encrypted copy — restores with your passphrase", "2/4");
    await R.moveTo('input[placeholder="Your passphrase"]'); await R.type('input[placeholder="Your passphrase"]', PASS); await R.dwell(400);
    await R.click('button:has-text("Download encrypted backup")'); await page.waitForTimeout(1100); await R.ensure();
    await R.cap("Forgot your passphrase? There’s no reset — that’s why you saved the Kit", "3/4"); await R.dwell(1700);
    await R.click('button:has-text("Lock")'); await page.waitForTimeout(1000); await R.ensure();
    await R.cap("Lock seals your data — it isn’t deleted", "3/4"); await R.dwell(1400);
    await R.click('button:has-text("Use recovery key")'); await page.waitForTimeout(700); await R.ensure();
    await page.locator("input[type=text], input[type=password]").first().fill(RK);
    await R.cap("Unlock with your Recovery Key — dashes and case don’t matter", "4/4"); await R.dwell(900);
    await R.click('button:has-text("Unlock")'); await page.waitForTimeout(1600); await R.ensure();
    await R.cap("Back in — your data was sealed, never lost", "4/4"); await R.dwell(2200); await done(h);
  },
  // 10 vaults — the PUBLIC publish story (demo SET UP + model connected; three docs pre-seeded).
  // Single-instance and fully authentic: create a vault, publish it PUBLIC (no key, no take-backs),
  // watch the card gain a Public badge + real SB- publisher fingerprint + version, then export the
  // next version. No key is DOM-redacted here because a PUBLIC vault has none; the passphrase field
  // is type=password (browser-masked), the same as clips 02/09.
  "10": async () => {
    const h = await open({ acceptDownloads: true });
    const { page, R } = h;
    await nav(page, "Knowledge").click(); await page.waitForTimeout(800); await R.ensure();
    await R.cap("A vault is a named set of documents — publish it for anyone to read", "1/5"); await R.dwell(1200);
    await R.scrollCenter('input[aria-label="Select Apartment Lease"]');
    await R.cap("Tick the documents that belong together", "1/5");
    await R.click('input[aria-label="Select Apartment Lease"]'); await R.dwell(300);
    await R.click('input[aria-label="Select Renters insurance policy"]'); await page.waitForTimeout(400); await R.ensure(); await R.dwell(400);
    await R.cap("Name a vault — it's created with your selection", "2/5");
    await R.scrollCenter('input[placeholder="New vault name…"]');
    await R.type('input[placeholder="New vault name…"]', "Lease papers"); await R.dwell(400);
    await R.click('button:has-text("Create with")'); await page.waitForTimeout(900); await R.ensure();
    await R.cap("Your vault is created — now share it, or publish it public", "2/5"); await R.dwell(1100);
    // Share -> Public: the no-key / no-take-backs warning sits BEFORE the export.
    await R.cap("Publish it public", "3/5");
    await R.scrollCenter('button:has-text("Share")'); await R.dwell(300);
    await R.click('button:has-text("Share")'); await page.waitForTimeout(600); await R.ensure();
    await R.click('input[type="radio"][value="open"]'); await page.waitForTimeout(400); await R.ensure();
    await R.scrollCenter(".share .warn");
    await R.highlight(".share .warn");
    await R.cap("Public means no key — and no taking it back", "3/5"); await R.dwell(1800); await R.noring();
    // Confirm with the passphrase (masked), then Export.
    await R.cap("Confirm with your passphrase, then Export", "4/5");
    await R.type('input[placeholder="Your passphrase"]', PASS); await R.dwell(400);
    await R.click('.share button:has-text("Export")');
    await R.cap("Publishing…", "4/5"); await page.waitForTimeout(700); // neutral beat: the card publishes while this shows
    await page.waitForSelector('.share button:has-text("Export update")', { timeout: 8000 }); await R.ensure();
    // The card now carries the Public chip + the real SB- fingerprint + the published version.
    await R.scrollCenter('.vrow .chip:has-text("Public")'); await page.waitForTimeout(300);
    await R.highlight('.vrow .chip:has-text("Public")');
    await R.cap("Published — a Public badge, your SB-… fingerprint, and the version", "4/5"); await R.dwell(1900); await R.noring();
    // Snapshot the version so we can prove the next publish bumps it (a fresh vault starts at v1,
    // and each publish increments — so the first public version is already past v1).
    const verBefore = await page.evaluate(() => {
      const m = (document.querySelector(".vrow")?.textContent || "").match(/\bv(\d+)\b/);
      return m ? Number(m[1]) : 0;
    });
    // Publish the next version: the file is signed, so only this identity can update it.
    await R.cap("Publish a new version — it's signed, so only you can", "5/5");
    await R.scrollCenter('.share button:has-text("Export update")'); await R.dwell(300);
    await R.type('input[placeholder="Your passphrase"]', PASS); await R.dwell(400);
    await R.click('.share button:has-text("Export update")'); await page.waitForTimeout(700);
    await page.waitForFunction((prev) => {
      const m = (document.querySelector(".vrow")?.textContent || "").match(/\bv(\d+)\b/);
      return !!m && Number(m[1]) > prev;
    }, verBefore, { timeout: 8000 }); await R.ensure();
    await R.scrollCenter('.vrow .chip:has-text("Public")'); await page.waitForTimeout(300);
    await R.highlight('.vrow .chip:has-text("Public")');
    await R.cap("The version bumps automatically — subscribers pick it up on their next check", "5/5"); await R.dwell(2000); await done(h);
  },
  // 11 vaults — the PUBLIC SUBSCRIBE + UPDATE story (the half clip 10 doesn't cover). Fully
  // authentic: a REAL open .sbvault (built by vault_format.pack, signed by one Ed25519 key) is served
  // in place of the network fetch by a RECORDER-ONLY netguard shim (gated on SB_GIFDEMO=1, lives in
  // tools/gifgen only — never in the shipped image; see gifdemo_shim/sitecustomize.py). Everything
  // the UI shows is real: the app verifies the publisher signature, PINS the key on first contact,
  // re-encrypts every doc under the subscriber's own master key, honours a real Detach, and applies
  // a real update whose summary counts updated / added / kept-yours. No passphrase is entered
  // anywhere in this flow (subscribe/detach/check/update are unlock-only), so nothing is redacted.
  "11": async () => {
    const SERVE = require("path").join(__dirname, "out", "gifdemo", "serve.txt");
    const serve = (name) => require("fs").writeFileSync(SERVE, name + "\n"); // flip the "hosted" file
    serve("publisher-v1.sbvault"); // start on v1 (run.sh resets it too)
    const SUB_URL = "https://vaults.example/frontend-playbook.sbvault";
    const h = await open();
    const { page, R } = h;
    await nav(page, "Knowledge").click(); await page.waitForTimeout(700); await R.ensure();
    await R.cap("Someone published a public vault — subscribe by URL", "1/4"); await R.dwell(1000);
    await R.scrollCenter('summary:has-text("Add someone else")');
    await R.click('summary:has-text("Add someone else")'); await page.waitForTimeout(350); await R.ensure();
    await R.moveTo('input[aria-label="Public vault URL"]');
    await R.type('input[aria-label="Public vault URL"]', SUB_URL); await R.dwell(350);
    await R.cap("Paste the link — a public vault needs no key", "1/4");
    await R.click('button:has-text("Subscribe")');
    await R.cap("Verifying the publisher's signature, re-encrypting under your key…", "1/4");
    await page.waitForSelector('.chip:has-text("Subscribed")', { timeout: 8000 }); await R.ensure();
    await R.scrollCenter('.vault .vrow'); await page.waitForTimeout(250);
    await R.highlight('.vault .vrow .chip.mono'); // the pinned publisher fingerprint
    await R.cap("Subscribed — the publisher's SB-… fingerprint is pinned, docs land", "2/4"); await R.dwell(1600); await R.noring();
    // Prove the docs really landed and are usable: a keyword search scoped to just this vault. The
    // search box sits directly above the vault, so this is a short hop, not a page-length scroll.
    await R.click('button:has-text("Search this")'); await page.waitForTimeout(250); await R.ensure();
    await R.type('input[aria-label="Search your knowledge"]', "on-call"); await R.dwell(200);
    await page.locator('select[aria-label="Search mode"]').selectOption({ label: "Keyword" }).catch(() => {});
    await R.click('form button:has-text("Search")'); await page.waitForTimeout(900); await R.ensure();
    await R.cap("Its documents landed — searchable, scoped to just this vault", "2/4"); await R.dwell(1500);
    // Make one document YOURS so the update must not overwrite it (the real owner-edit protection).
    await R.scrollCenter('.vault .vrow'); await page.waitForTimeout(250);
    await R.click('.vault button.linklike:has-text("document")'); await page.waitForTimeout(400); await R.ensure();
    await R.cap("Make one copy yours — an update must never overwrite it", "3/4");
    await R.scrollCenter('.vmembers li:has-text("Onboarding checklist")');
    await R.click('.vmembers li:has-text("Onboarding checklist") button:has-text("Detach")');
    await page.waitForTimeout(600); await R.ensure(); await R.dwell(700);
    // The publisher ships v2 — the ONE simulated step: flip which local file the shim serves.
    serve("publisher-v2.sbvault");
    await R.cap("The publisher ships a new version — check for updates", "4/4");
    await R.scrollCenter('button:has-text("Check for updates")'); await page.waitForTimeout(250);
    await R.click('button:has-text("Check for updates")');
    await page.waitForSelector('button:has-text("Update now")', { timeout: 8000 }); await R.ensure();
    await R.scrollCenter('.upd'); await R.highlight('.upd');
    await R.cap("Update available (v1 → v2) — signed by the same pinned key", "4/4"); await R.dwell(1500); await R.noring();
    await R.click('button:has-text("Update now")'); await page.waitForTimeout(1300); await R.ensure();
    await R.scrollCenter('.upd'); await R.highlight('.upd');
    await R.cap("Updated and added — and the copy you made yours was kept", "4/4"); await R.dwell(2200); await R.noring();
    await done(h);
  },
};

(async () => {
  const n = process.argv[2];
  if (!CLIPS[n]) { console.error("usage: node clips.js <01..11>"); process.exit(1); }
  await CLIPS[n]();
  console.log("recorded " + n);
})();
