// What to do when a resumed tab discovers the backend's version changed under it.
//
// Mobile reality (field report, v0.9.7 upgrade): the layout's 60s version watcher
// freezes while a PWA is backgrounded and iOS throttles it on resume, so a resumed
// phone ran old code until a force-close. The check now runs at the moments that
// matter (foreground return), and THIS decides what happens on a change:
//   - vault LOCKED  -> reload NOW. Locked is the guaranteed post-update state; every
//     feature needs the key, so there is nothing in flight a reload could lose.
//   - vault unlocked -> show the banner and let the user choose; auto-reloading over
//     someone's half-typed message to save them a tap is a bad trade.
export function resumeUpdateAction(
  loadedVersion: string,
  currentVersion: string,
  unlocked: boolean,
): "reload" | "banner" | "none" {
  if (!loadedVersion || !currentVersion || loadedVersion === currentVersion) return "none";
  return unlocked ? "banner" : "reload";
}
