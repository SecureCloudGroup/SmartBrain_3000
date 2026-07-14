// Record one quickstart clip:  node clips.js <NN>   (NN = 01..09).  See README.md
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
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 2, recordVideo: { dir: "video", size: { width: W, height: H } }, ...opts });
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
    await page.getByRole("link", { name: "Cloud providers" }).first().click().catch(() => {}); await page.waitForTimeout(700); await R.ensure();
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
    await R.cap("Press Send", "1/3"); await R.click('button:has-text("Send")');
    await R.cap("It answers using your connected model", "2/3"); await page.waitForTimeout(3800); await R.ensure();
    await R.cap("A real reply — your model is working", "2/3"); await R.dwell(2400);
    await R.cap("It reads freely — anything that changes data waits for approval", "3/3"); await R.dwell(2200); await done(h);
  },
  // 04 knowledge (demo SET UP + model connected).
  "04": async () => {
    const h = await open(); const { page, R } = h;
    const LEASE = "Lease term 12 months, rent $1,800/mo due on the 1st, 60-day notice to vacate, landlord Pat Rivera.";
    await nav(page, "Knowledge").click(); await page.waitForTimeout(800); await R.ensure();
    await R.cap("Add private knowledge — encrypted on your machine", "1/4"); await R.dwell(1500);
    await page.locator("text=write a note").first().click().catch(() => {}); await page.waitForTimeout(500);
    await R.type("#t", "Apartment Lease"); await R.type("#c", LEASE); await R.dwell(500);
    await page.locator('xpath=//*[@id="c"]/following::button[contains(.,"Add")][1]').click(); await page.waitForTimeout(900);
    await R.cap("Reindex so semantic search can find it", "2/4"); await R.click('button:has-text("Reindex")'); await page.waitForTimeout(1500); await R.ensure();
    await R.cap("Search by meaning, not exact words", "3/4");
    await page.locator("select").filter({ hasText: "Meaning" }).first().selectOption({ label: "Meaning" }).catch(() => {});
    await R.type('input[placeholder^="Search your knowledge"]', "what are my lease terms?"); await R.dwell(400);
    await R.click('button:has-text("Search")'); await page.waitForTimeout(1200); await R.ensure(); await R.dwell(1400);
    await nav(page, "Chat").click(); await page.waitForTimeout(800); await R.ensure();
    await R.cap("Or just ask Chat — it reads your knowledge for you", "4/4");
    await R.type(MSG, "What does my knowledge say about my lease?"); await R.dwell(400);
    await R.click('button:has-text("Send")'); await page.waitForTimeout(3800); await R.ensure();
    await R.cap("Answered from your knowledge — no approval needed to read", "4/4"); await R.dwell(2400); await done(h);
  },
  // 05 approval loop (demo SET UP + model connected; empty Activity/Planner).
  "05": async () => {
    const h = await open(); const { page, R } = h;
    await R.cap("Anything that changes data waits for your OK", "1/4"); await R.dwell(1700);
    await R.cap("Ask it to change something", "1/4");
    await R.click("button:has-text(\"buy milk\")"); await R.dwell(400);
    await R.click('button:has-text("Send")'); await page.waitForTimeout(2800); await R.ensure();
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
    await page.getByRole("link", { name: "Remote access" }).first().click(); await page.waitForTimeout(700); await R.ensure();
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
    await page.getByRole("link", { name: "Account & Data" }).first().click(); await page.waitForTimeout(700); await R.ensure();
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
};

(async () => {
  const n = process.argv[2];
  if (!CLIPS[n]) { console.error("usage: node clips.js <01..09>"); process.exit(1); }
  await CLIPS[n]();
  console.log("recorded " + n);
})();
