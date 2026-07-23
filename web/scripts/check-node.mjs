// Preflight for `npm test` (wired as the `pretest` script): refuse to run the suite on a
// Node that jsdom can't load. jsdom 29 needs require(esm) — Node ^20.19 / ^22.12 / >=24
// (mirrors jsdom's engines field). On anything older, vitest's jsdom workers fail to
// START and vitest still exits 0 — so whole test files (including the markdown
// XSS-sanitizer pins) are SILENTLY skipped. Fail loud here instead.
const [major, minor] = process.versions.node.split(".").map(Number);
const ok = (major === 20 && minor >= 19) || (major === 22 && minor >= 12) || major >= 24;
if (!ok) {
  console.error(
    `\nNode ${process.versions.node} can't run the jsdom-based tests (needs ^20.19 / ^22.12 / >=24).\n` +
      "Without this check, vitest would silently skip those files and still exit 0.\n" +
      "Fix: `nvm use` (this repo's .nvmrc says 22), then rerun.\n",
  );
  process.exit(1);
}
