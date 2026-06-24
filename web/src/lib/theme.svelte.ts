// Light/dark theme. "system" (follow the OS, the default) | "light" | "dark".
// app.html applies a saved choice to <html data-theme> before first paint to
// avoid a flash; app.css defines the palettes. The toggle cycles system→light→dark.

type Mode = "system" | "light" | "dark";

export const theme = $state<{ mode: Mode }>({ mode: "system" });

function read(): Mode {
  try {
    const t = localStorage.getItem("theme");
    return t === "light" || t === "dark" ? t : "system";
  } catch {
    return "system";
  }
}

function apply(mode: Mode) {
  const el = document.documentElement;
  if (mode === "system") el.removeAttribute("data-theme");
  else el.dataset.theme = mode;
}

export function initTheme() {
  theme.mode = read();
}

export function cycleTheme() {
  theme.mode = theme.mode === "system" ? "light" : theme.mode === "light" ? "dark" : "system";
  try {
    if (theme.mode === "system") localStorage.removeItem("theme");
    else localStorage.setItem("theme", theme.mode);
  } catch {
    /* storage unavailable — the choice still applies for this session */
  }
  apply(theme.mode);
}
