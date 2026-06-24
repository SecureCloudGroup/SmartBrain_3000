import path from "node:path";

import { defineConfig } from "vitest/config";

// Unit tests for the pure remote-access logic (protocol framing, channel binding,
// Ed25519 verification, pairing payload). Node environment — crypto.subtle, atob/btoa,
// and TextEncoder are all available in Node 20, so no DOM is needed. The setup file
// stubs SvelteKit virtual modules ($app/*) that aren't resolvable outside a SK build.
export default defineConfig({
  resolve: {
    alias: {
      $lib: path.resolve(__dirname, "src/lib"),
    },
  },
  test: {
    environment: "node",
    include: ["src/lib/**/*.test.ts"],
    setupFiles: ["src/vitest-setup.ts"],
  },
});
