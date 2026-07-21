// Review switcher (theme × accent × desktop-nav variant, persisted across pages) and a
// shared inline icon sprite. No build step, no dependencies. Icons are hand-authored
// 24×24 stroke primitives (the app itself vendors a real icon set at Stage 4).
(function () {
  // ---- icon sprite (referenced as <svg><use href="#i-name"/></svg>) ----
  const ICONS = {
    chat: '<path d="M4 5h16v12H9l-5 4z"/>',
    book: '<path d="M5 3h13a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H7a2 2 0 0 1-2-2z"/><path d="M19 16H7a2 2 0 0 0-2 2"/>',
    tasks: '<path d="M4 6h2M4 12h2M4 18h2M9.5 6H20M9.5 12H20M9.5 18H20"/>',
    clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5v4.8l3 1.8"/>',
    mail: '<rect x="3" y="5.5" width="18" height="13" rx="1.5"/><path d="M3.5 7l8.5 6 8.5-6"/>',
    pulse: '<path d="M3 12.5h4l3-7 4 13.5 3-6.5h4"/>',
    chart: '<path d="M5 20v-9M12 20V5M19 20v-6"/>',
    sliders: '<path d="M4 8h16M4 16h16"/><circle cx="14.5" cy="8" r="2.4" fill="var(--bg)"/><circle cx="8.5" cy="16" r="2.4" fill="var(--bg)"/>',
    help: '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.3a2.5 2.5 0 1 1 3.5 2.4c-.8.35-1.1.9-1.1 1.8"/><path d="M12 16.6v.2"/>',
    lock: '<rect x="6" y="10.5" width="12" height="9" rx="1.5"/><path d="M9 10.5V8a3 3 0 0 1 6 0v2.5"/>',
    shield: '<path d="M12 3l7 2.8v5.4c0 4.4-2.9 7.4-7 9-4.1-1.6-7-4.6-7-9V5.8z"/><path d="M9.2 12l2 2 3.8-3.8"/>',
    send: '<path d="M21 3L10.8 13.2M21 3l-6.6 18-3.6-8.4L2.4 9z"/>',
    stop: '<rect x="7" y="7" width="10" height="10" rx="1.5"/>',
    copy: '<rect x="9" y="9" width="11" height="11" rx="1.5"/><path d="M5.5 14.5H5a1.5 1.5 0 0 1-1.5-1.5V5A1.5 1.5 0 0 1 5 3.5h8A1.5 1.5 0 0 1 14.5 5v.5"/>',
    refresh: '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 4v4.5h-4.5"/>',
    pencil: '<path d="M4.5 19.5l.9-3.6L16.8 4.5a1.4 1.4 0 0 1 2 0l.7.7a1.4 1.4 0 0 1 0 2L8.1 18.6z"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M20.5 20.5L16 16"/>',
    plus: '<path d="M12 5.5v13M5.5 12h13"/>',
    x: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    check: '<path d="M5 12.8l4.2 4.2L19 7"/>',
    chevdown: '<path d="M6.5 9.5l5.5 5.5 5.5-5.5"/>',
    moreh: '<circle cx="5.5" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="18.5" cy="12" r="1.4" fill="currentColor" stroke="none"/>',
    file: '<path d="M6 3h7.5L18 7.5V21H6z"/><path d="M13.5 3v4.5H18"/>',
    vault: '<path d="M3.5 5h17v4h-17z"/><path d="M5.5 9v10.5h13V9"/><path d="M10 13.5h4"/>',
    upload: '<path d="M12 15.5V4.5M6.8 9.7L12 4.5l5.2 5.2"/><path d="M4.5 19.5h15"/>',
    warn: '<path d="M12 3.5L21.5 20h-19z"/><path d="M12 10v4.4M12 17.3v.4"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M19.1 4.9l-1.8 1.8M6.7 17.3l-1.8 1.8"/>',
    moon: '<path d="M20 13.5A8.5 8.5 0 1 1 10.5 4a7 7 0 0 0 9.5 9.5z"/>',
    arrdown: '<path d="M12 5v13M6 12.5l6 6 6-6"/>',
  };
  const symbols = Object.entries(ICONS)
    .map(([n, p]) => `<symbol id="i-${n}" viewBox="0 0 24 24">${p}</symbol>`)
    .join("");
  const sprite = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  sprite.setAttribute("style", "display:none");
  sprite.innerHTML = symbols;
  document.body.prepend(sprite);
  // stroke defaults for every icon use
  const style = document.createElement("style");
  style.textContent =
    "svg.ic{fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;width:16px;height:16px;vertical-align:-0.18em}";
  document.head.append(style);

  // ---- switcher state ----
  const root = document.documentElement;
  const KEYS = { theme: "sb-mock-theme", accent: "sb-mock-accent", nav: "sb-mock-nav" };

  const apply = () => {
    root.dataset.theme = localStorage.getItem(KEYS.theme) || "dark";
    root.dataset.accent = localStorage.getItem(KEYS.accent) || "teal";
    const nav = localStorage.getItem(KEYS.nav) || "sidebar";
    document.querySelectorAll(".shell").forEach((s) => s.classList.toggle("topbar-mode", nav === "topbar"));
    document.querySelectorAll(".switcher .grp button").forEach((b) => {
      const { k, v } = b.dataset;
      const cur = k === "nav" ? nav : root.dataset[k];
      b.classList.toggle("on", cur === v);
    });
  };

  document.addEventListener("click", (e) => {
    const b = e.target.closest(".switcher .grp button");
    if (!b) return;
    localStorage.setItem(KEYS[b.dataset.k], b.dataset.v);
    apply();
  });

  apply();
})();
