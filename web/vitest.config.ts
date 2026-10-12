import path from "node:path";

import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vitest/config";

// Unit tests for the pure remote-access logic (protocol framing, channel binding,
// Ed25519 verification, pairing payload). Node environment — crypto.subtle, atob/btoa,
// and TextEncoder are all available in Node 20, so no DOM is needed. The setup file
// stubs SvelteKit virtual modules ($app/*) that aren't resolvable outside a SK build.
// Component DOM tests (src/lib/components/*.test.ts, `// @vitest-environment jsdom`) mount
// .svelte files: svelte() compiles them with their CSS injected (emitCss: false), so jsdom
// computes their scoped styles, and the "browser" condition resolves `svelte` to its client
// runtime (the default condition loads the server build, where mount() throws).
export default defineConfig({
  plugins: [svelte({ emitCss: false })],
  resolve: {
    alias: {
      $lib: path.resolve(__dirname, "src/lib"),
    },
    conditions: ["browser"],
  },
  test: {
    environment: "node",
    include: ["src/lib/**/*.test.ts"],
    setupFiles: ["src/vitest-setup.ts"],
  },
});
