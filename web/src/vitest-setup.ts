// Global stubs for vitest. SvelteKit virtual modules ($app/*, $service-worker) are not
// resolvable outside a SvelteKit build; tests that exercise code which imports them get a
// no-op stub here. Per-test files may still override with vi.mock for assertion purposes.

import { vi } from "vitest";

vi.mock("$app/navigation", () => ({
  goto: vi.fn(),
  invalidate: vi.fn(),
  invalidateAll: vi.fn(),
}));

vi.mock("$app/environment", () => ({
  browser: false,
  dev: false,
  building: false,
}));

// sw-bridge pulls in connection.svelte.ts (uses $state) which can't run in plain Node.
// Tests that exercise sw-bridge directly override this mock locally; everywhere else
// this stub keeps api.ts's `await remoteReady` from dragging in the Svelte rune.
vi.mock("$lib/remote/sw-bridge", () => ({
  remoteReady: Promise.resolve(),
  initRemote: () => Promise.resolve(),
  watchForSWUpdate: () => {},
}));
