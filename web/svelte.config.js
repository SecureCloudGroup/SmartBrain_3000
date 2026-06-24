import adapter from "@sveltejs/adapter-static";
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

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
