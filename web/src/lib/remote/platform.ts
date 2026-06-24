// Lightweight runtime platform checks for the mobile setup flow.

// True when running as the installed Home-Screen app (standalone display), where iOS gives us
// our own storage — the context we want the user to pair in.
export function isInstalledApp(): boolean {
  if (typeof window === "undefined") return false;
  const standalone = window.matchMedia?.("(display-mode: standalone)")?.matches ?? false;
  return standalone || (window.navigator as { standalone?: boolean }).standalone === true;
}

// True on a phone/tablet browser (so we can steer the user to install first, not pair in-browser).
export function isMobile(): boolean {
  if (typeof navigator === "undefined") return false;
  return /iphone|ipad|ipod|android/i.test(navigator.userAgent);
}
