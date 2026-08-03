// Preflight for `npm test` (wired as the `pretest` script): refuse to run the suite on a
// Node that jsdom can't load. jsdom 30 needs ^22.22.2 / ^24.15.0 / >=26 (mirrors jsdom's
// engines field; it dropped Node 20 outright and raised the 22.x floor). On anything
// older, vitest's jsdom workers fail to START and vitest still exits 0 — so whole test
// files (including the markdown XSS-sanitizer pins) are SILENTLY skipped. Fail loud here.
//
// Re-check this against jsdom's own engines on every bump. A dependency update moves the
// real requirement but cannot move this file, so the two drift apart silently and the
// guard begins admitting exactly the versions it exists to reject.
const [major, minor, patch] = process.versions.node.split(".").map(Number);
const ok =
  (major === 22 && (minor > 22 || (minor === 22 && patch >= 2))) ||
  (major === 24 && minor >= 15) ||
  major >= 26;
if (!ok) {
  console.error(
    `\nNode ${process.versions.node} can't run the jsdom-based tests (needs ^22.22.2 / ^24.15.0 / >=26).\n` +
      "Without this check, vitest would silently skip those files and still exit 0.\n" +
      "Fix: `nvm use` (this repo's .nvmrc says 22), then rerun.\n",
  );
  process.exit(1);
}
