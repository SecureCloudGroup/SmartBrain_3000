// Reusable GIF recorder: synthetic cursor (eased), lower-third caption band,
// step pill, highlight ring, click ripple, and full-screen title/terminal cards.
// All overlays are DOM injected into the page so they're captured in the video.
// See README.md for the pipeline. Requires: playwright.
const { chromium } = require("playwright");

const OVERLAY = () => {
  if (window.__recInit) return;
  window.__recInit = true;
  const L = document.createElement("div");
  L.id = "__rec";
  L.style.cssText =
    "position:fixed;inset:0;z-index:2147483646;pointer-events:none;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif";
  L.innerHTML = `
    <div id="__card" style="position:absolute;inset:0;background:#0b0e13;color:#e6edf3;display:none;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:0 64px"></div>
    <div id="__cap" style="position:absolute;left:0;right:0;bottom:0;height:76px;background:rgba(14,17,22,.88);color:#fff;display:flex;align-items:center;justify-content:center;font-size:25px;font-weight:600;padding:0 48px;text-align:center;box-sizing:border-box"></div>
    <div id="__pill" style="position:absolute;left:18px;top:16px;background:rgba(14,17,22,.9);color:#fff;font-size:15px;font-weight:700;padding:5px 12px;border-radius:999px;display:none"></div>
    <div id="__ring" style="position:absolute;border:3px solid #6366f1;border-radius:10px;box-shadow:0 0 0 4px rgba(99,102,241,.22),0 0 16px 2px rgba(99,102,241,.55);opacity:0;transition:all .28s ease;box-sizing:border-box"></div>
    <div id="__cur" style="position:absolute;left:-60px;top:-60px;transition:left .55s cubic-bezier(.45,.05,.2,1),top .55s cubic-bezier(.45,.05,.2,1);filter:drop-shadow(0 1px 2px rgba(0,0,0,.45))">
      <svg width="26" height="26" viewBox="0 0 24 24"><path d="M5 3 L5 19 L9.2 15 L12 21 L14.6 19.9 L11.7 14.2 L17 14.2 Z" fill="#fff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/></svg></div>`;
  document.body.appendChild(L);
  const $ = (id) => document.getElementById(id);
  window.__rec = {
    cap: (t, s) => { $("__cap").textContent = t || ""; const p = $("__pill"); if (s) { p.textContent = s; p.style.display = "block"; } else p.style.display = "none"; },
    cur: (x, y, ms) => { const c = $("__cur"); c.style.transitionDuration = (ms || 550) + "ms"; c.style.left = x + "px"; c.style.top = y + "px"; },
    ring: (x, y, w, h) => { const r = $("__ring"); r.style.left = (x - 6) + "px"; r.style.top = (y - 6) + "px"; r.style.width = (w + 12) + "px"; r.style.height = (h + 12) + "px"; r.style.opacity = "1"; },
    noring: () => { $("__ring").style.opacity = "0"; },
    ripple: (x, y) => { const d = document.createElement("div"); d.style.cssText = `position:absolute;left:${x}px;top:${y}px;width:10px;height:10px;margin:-5px 0 0 -5px;border:2.5px solid #6366f1;border-radius:50%;opacity:.9;transition:all .55s ease`; $("__rec").appendChild(d); requestAnimationFrame(() => { d.style.width = "48px"; d.style.height = "48px"; d.style.margin = "-24px 0 0 -24px"; d.style.opacity = "0"; }); setTimeout(() => d.remove(), 680); },
    card: (html, show) => { const c = $("__card"); if (show) { c.innerHTML = html; c.style.display = "flex"; } else c.style.display = "none"; },
  };
};

class Rec {
  constructor(page) { this.page = page; }
  async init() { await this.page.evaluate(OVERLAY); }
  async ensure() { await this.page.evaluate(OVERLAY); }
  async cap(t, s) { await this.page.evaluate(([t, s]) => window.__rec.cap(t, s), [t, s]); }
  async dwell(ms) { await this.page.waitForTimeout(ms); }
  async _center(sel) { const el = this.page.locator(sel).first(); await el.scrollIntoViewIfNeeded().catch(() => {}); const b = await el.boundingBox(); return b ? { x: b.x + b.width / 2, y: b.y + b.height / 2, b } : null; }
  async moveTo(sel, ms = 600) { const c = await this._center(sel); if (!c) return null; await this.page.evaluate(([x, y, ms]) => window.__rec.cur(x, y, ms), [c.x, c.y, ms]); await this.dwell(ms + 120); return c; }
  async click(sel, ms = 600) { const c = await this.moveTo(sel, ms); if (!c) return; await this.dwell(180); await this.page.evaluate(([x, y]) => window.__rec.ripple(x, y), [c.x, c.y]); await this.dwell(140); await this.page.locator(sel).first().click(); }
  async highlight(sel) { const c = await this._center(sel); if (!c) return; await this.page.evaluate(([x, y, w, h]) => window.__rec.ring(x, y, w, h), [c.b.x, c.b.y, c.b.width, c.b.height]); }
  async noring() { await this.page.evaluate(() => window.__rec.noring()); }
  async scrollCenter(sel) { await this.page.locator(sel).first().evaluate((e) => e.scrollIntoView({ block: "center", inline: "nearest" })).catch(() => {}); await this.dwell(400); }
  async type(sel, val) { await this.page.locator(sel).first().fill(val); }
  async card(html, show = true) { await this.page.evaluate(([h, s]) => window.__rec.card(h, s), [html, show]); }
}
module.exports = { chromium, Rec, OVERLAY };
