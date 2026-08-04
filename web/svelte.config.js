import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import adapter from "@sveltejs/adapter-static";
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

// Inputs that decide what the build emits. Anything read here must be a real
// input: adding an output would make the version depend on itself.
const VERSION_INPUTS = ["src", "static", "package-lock.json", "svelte.config.js", "vite.config.ts"];

/**
 * A build id derived from the source, not the clock.
 *
 * SvelteKit's default version is Date.now(). That id is baked into a client chunk
 * (it backs the update check), so its hash moves on every build, and the chunks
 * importing it move with it — two builds of identical source differed in ~70
 * filenames. That made the shipped bundle in app/smartbrain_3000/web/ impossible
 * to diff against a fresh build, which is why it silently went stale: source and
 * shipped UI diverged for a whole release with CI green throughout.
 *
 * Hashing the inputs instead makes the build reproducible, so CI can assert that
 * what is committed is what the source produces. It is also the more correct id
 * for the purpose it serves: the service-worker cache and the "new version
 * available" check now roll when the app actually changes, rather than on every
 * rebuild of identical code.
 */
function sourceVersion() {
  const hash = createHash("sha256");
  const walk = (path) => {
    const stat = statSync(path);
    if (stat.isDirectory()) {
      for (const entry of readdirSync(path).sort()) walk(join(path, entry)); // sort: stable order
      return;
    }
    hash.update(path); // include the name, so a pure rename still changes the id
    hash.update(readFileSync(path));
  };
  for (const input of VERSION_INPUTS) {
    // Optional by design: docs.generated.ts is written by `npm run docs` just
    // before this runs, and a missing optional input must not fail the build.
    try {
      walk(input);
    } catch {
      continue;
    }
  }
  return hash.digest("hex").slice(0, 16);
}

// Pure client-rendered SPA (ssr=false in the root layout) emitted as static
// files into the FastAPI-served directory. The "fallback" page is the SPA
// entry FastAPI returns for any client route. CSP is generated in hash mode so
// SvelteKit's own inline bootstrap is allow-listed by hash (script-src 'self'),
// not by 'unsafe-inline' — see app/smartbrain_3000/serving.py for how the
// backend defers page CSP to this meta policy.
/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    version: { name: sourceVersion() },
    adapter: adapter({
      pages: "../app/smartbrain_3000/web",
      assets: "../app/smartbrain_3000/web",
      fallback: "index.html",
      precompress: false,
      strict: false,
    }),
    csp: {
      mode: "hash",
      directives: {
        "default-src": ["self"],
        "script-src": ["self"],
        "style-src": ["self", "unsafe-inline"],
        "img-src": ["self", "data:"],
        // Remote access (WebRTC): the phone opens a wss:// to the operator's
        // signaling broker and stun:/turn: to coturn — cross-origin and non-https
        // schemes, so 'self' alone blocks them. Scheme-sources (not a hardcoded
        // host) keep this build operator-agnostic. wss: is a negligible widening:
        // an XSS already has same-origin /api access to everything.
        "connect-src": ["self", "wss:", "stun:", "turn:"],
        "manifest-src": ["self"],
        "object-src": ["none"],
        "base-uri": ["self"],
        "form-action": ["self"],
        "frame-ancestors": ["none"],
      },
    },
  },
};

export default config;
