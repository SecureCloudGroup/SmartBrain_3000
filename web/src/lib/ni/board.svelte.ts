// Board store for /ni: the item list, load(), the soft 10 s poll, the bounded 3 s fast poll
// while a card walks a creation flow, visibility handling, and the `gen` stale-response guard.
// Dependencies are injected so the timing rules are unit-testable with fake timers.
import type { NiBoardItem } from "$lib/api";
import { isFlowActive } from "$lib/ni/flow";

export const SLOW_POLL_MS = 10_000;
export const FAST_POLL_MS = 3_000;

export interface BoardDeps {
  fetchBoard: () => Promise<{ items: NiBoardItem[] }>;
  isUnlocked: () => boolean;
  // Called for every non-stale load outcome: null on success, else the raw error (the page
  // turns it into words with describeError — kept out of here so the store has no api import).
  onLoad?: (err: unknown) => void;
}

export function createBoard(deps: BoardDeps) {
  console.assert(typeof deps.fetchBoard === "function", "createBoard: fetchBoard present");
  console.assert(typeof deps.isUnlocked === "function", "createBoard: isUnlocked present");
  let items = $state<NiBoardItem[]>([]);
  let loaded = $state(false);
  let lastError = $state<unknown>(null);
  // Request-generation counter — a stale response never overwrites newer state (e.g. a
  // delete + re-poll racing an earlier slow fetch would resurrect the card).
  let gen = 0;
  // A boolean derive: the fast-poll effect re-runs only when this flips, not on every load.
  const anyActive = $derived(items.some((it) => isFlowActive(it.flow ?? null)));
  let slow: ReturnType<typeof setInterval> | null = null;
  let stopRoot: (() => void) | null = null;

  async function load(): Promise<void> {
    console.assert(typeof deps.fetchBoard === "function", "load: fetchBoard present");
    console.assert(gen >= 0, "load: gen counter present");
    if (!deps.isUnlocked()) return;
    const g = ++gen;
    try {
      const r = await deps.fetchBoard();
      if (g !== gen) return;
      // position asc — the spec orders the board that way; the server MAY already sort,
      // but a client-side re-sort keeps a mid-migration server honest.
      items = [...r.items].sort((a, b) => a.position - b.position);
      lastError = null;
      deps.onLoad?.(null);
    } catch (err) {
      if (g !== gen) return;
      lastError = err;
      deps.onLoad?.(err);
    } finally {
      if (g === gen) loaded = true;
    }
  }

  function tick(): void {
    if (document.visibilityState === "visible") void load();
  }

  function start(): void {
    console.assert(typeof document !== "undefined", "start: DOM available");
    if (slow !== null) return;
    slow = setInterval(tick, SLOW_POLL_MS);
    document.addEventListener("visibilitychange", tick);
    stopRoot = $effect.root(() => {
      $effect(() => {
        if (!anyActive) return;
        const fast = setInterval(tick, FAST_POLL_MS);
        return () => clearInterval(fast);
      });
    });
  }

  function stop(): void {
    console.assert(typeof document !== "undefined", "stop: DOM available");
    if (slow !== null) clearInterval(slow);
    slow = null;
    document.removeEventListener("visibilitychange", tick);
    stopRoot?.();
    stopRoot = null;
  }

  return {
    get items() { return items; },
    set items(v: NiBoardItem[]) { items = v; },
    get loaded() { return loaded; },
    get lastError() { return lastError; },
    get anyActive() { return anyActive; },
    load,
    start,
    stop,
  };
}
