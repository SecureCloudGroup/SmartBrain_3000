// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, untrack } from "svelte";
import type { NiBoardItem, NiFlowPoll } from "$lib/api";
import { createBoard } from "./board.svelte";
import { runEffect } from "./effectRoot.svelte";

const setVisibility = (v: string) => {
  Object.defineProperty(document, "visibilityState", { configurable: true, value: v });
};

function item(id: string, position: number, flowState?: string): NiBoardItem {
  return { id, position, flow: flowState ? { state: flowState } : null } as unknown as NiBoardItem;
}

// A fake api whose responses the test resolves by hand, in any order.
function fakeApi(initial: NiBoardItem[] = []) {
  const pending: Array<{ resolve: (v: { items: NiBoardItem[] }) => void; reject: (e: unknown) => void }> = [];
  const fetchBoard = vi.fn(
    () => new Promise<{ items: NiBoardItem[] }>((resolve, reject) => pending.push({ resolve, reject })),
  );
  return {
    fetchBoard,
    pending,
    respond(i: number, items: NiBoardItem[] = initial) { pending[i].resolve({ items }); },
  };
}

const settle = async () => { await vi.advanceTimersByTimeAsync(0); flushSync(); };

describe("board store", () => {
  let cleanups: Array<() => void> = [];
  beforeEach(() => {
    vi.useFakeTimers();
    setVisibility("visible");
  });
  afterEach(() => {
    cleanups.forEach((c) => c());
    cleanups = [];
    vi.useRealTimers();
  });

  function make(api: ReturnType<typeof fakeApi>, unlocked = () => true) {
    const board = createBoard({ fetchBoard: api.fetchBoard, isUnlocked: unlocked });
    cleanups.push(() => board.stop());
    return board;
  }

  it("sorts by position and marks loaded", async () => {
    const api = fakeApi();
    const board = make(api);
    void board.load();
    api.respond(0, [item("b", 2), item("a", 1)]);
    await settle();
    expect(board.items.map((i) => i.id)).toEqual(["a", "b"]);
    expect(board.loaded).toBe(true);
  });

  it("does not fetch while locked", async () => {
    const api = fakeApi();
    const board = make(api, () => false);
    await board.load();
    expect(api.fetchBoard).not.toHaveBeenCalled();
  });

  it("drops a stale response that resolves after a newer one", async () => {
    const api = fakeApi();
    const board = make(api);
    void board.load();
    void board.load();
    api.respond(1, [item("new", 1)]);
    await settle();
    api.respond(0, [item("old", 1)]);
    await settle();
    expect(board.items.map((i) => i.id)).toEqual(["new"]);
  });

  it("keeps the poll error in lastError and clears it on success", async () => {
    const api = fakeApi();
    const board = make(api);
    void board.load();
    api.pending[0].reject(new Error("boom"));
    await settle();
    expect((board.lastError as Error).message).toBe("boom");
    void board.load();
    api.respond(1, []);
    await settle();
    expect(board.lastError).toBeNull();
  });

  it("slow-polls every 10 s while visible and stops after stop()", async () => {
    const api = fakeApi();
    const board = make(api);
    board.start();
    expect(api.fetchBoard).not.toHaveBeenCalled(); // start() alone never loads
    await vi.advanceTimersByTimeAsync(10_000);
    expect(api.fetchBoard).toHaveBeenCalledTimes(1);
    board.stop();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(api.fetchBoard).toHaveBeenCalledTimes(1);
  });

  it("fast-polls at 3 s only while a card is active, and not at all when idle", async () => {
    const api = fakeApi();
    const board = make(api);
    board.start();
    void board.load();
    api.respond(0, [item("a", 1)]);
    await settle();
    expect(board.anyActive).toBe(false);
    await vi.advanceTimersByTimeAsync(4_000);
    expect(api.fetchBoard).toHaveBeenCalledTimes(1); // idle: no 3 s ticks

    void board.load(); // call 2
    api.respond(1, [item("a", 1, "sampling")]);
    await settle();
    expect(board.anyActive).toBe(true);
    const before = api.fetchBoard.mock.calls.length;
    await vi.advanceTimersByTimeAsync(3_000);
    expect(api.fetchBoard.mock.calls.length).toBe(before + 1);

    // flow settles -> fast poll torn down
    api.respond(before, [item("a", 1, "failed")]);
    await settle();
    expect(board.anyActive).toBe(false);
    const after = api.fetchBoard.mock.calls.length;
    await vi.advanceTimersByTimeAsync(2_900);
    expect(api.fetchBoard.mock.calls.length).toBe(after);
  });

  it("does not recreate the fast interval after each reload", async () => {
    const api = fakeApi();
    const board = make(api);
    board.start();
    void board.load();
    api.respond(0, [item("a", 1, "mapping")]);
    await settle();
    const spy = vi.spyOn(globalThis, "setInterval");
    for (let n = 1; n <= 3; n++) {
      void board.load();
      api.respond(n, [item("a", 1, "mapping")]);
      await settle();
    }
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it("skips ticks while the tab is hidden and reloads on becoming visible", async () => {
    const api = fakeApi();
    const board = make(api);
    board.start();
    setVisibility("hidden");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(api.fetchBoard).not.toHaveBeenCalled();
    setVisibility("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(api.fetchBoard).toHaveBeenCalledTimes(1);
  });

  it("an effect that triggers load() under untrack loads once, never loops (#519)", async () => {
    const api = fakeApi();
    const board = make(api);
    const root = runEffect(() => { untrack(() => { void board.load(); }); });
    cleanups.push(root);
    await settle();
    expect(api.fetchBoard).toHaveBeenCalledTimes(1);
    api.respond(0, [item("a", 1)]);
    await settle();
    await vi.advanceTimersByTimeAsync(5_000);
    flushSync();
    expect(api.fetchBoard).toHaveBeenCalledTimes(1);
  });

  it("even a bare effect calling load() does not loop on its own reassignment", async () => {
    const api = fakeApi();
    const board = make(api);
    const root = runEffect(() => { void board.load(); });
    cleanups.push(root);
    await settle();
    api.respond(0, [item("a", 1)]);
    await settle();
    expect(api.fetchBoard).toHaveBeenCalledTimes(1);
  });
});

// v2: the per-item 3 s poll. Rows with `actions` are v2; a poll reads one item, not the board.
describe("board store, per-item poll (v2)", () => {
  let cleanups: Array<() => void> = [];
  beforeEach(() => { vi.useFakeTimers(); setVisibility("visible"); });
  afterEach(() => { cleanups.forEach((c) => c()); cleanups = []; vi.useRealTimers(); });

  const v2 = (id: string, position: number, flowState?: string) =>
    ({ ...item(id, position, flowState), actions: ["cancel"] }) as NiBoardItem;
  const poll = (id: string, flowState: string | null): NiFlowPoll =>
    ({ id, state: "draft", flow: flowState ? { state: flowState } : null, awaiting_yes: null, actions: ["cancel"] }) as unknown as NiFlowPoll;

  function setup(initial: NiBoardItem[]) {
    const api = fakeApi();
    const calls: string[] = [];
    const waiting: Array<{ id: string; resolve: (v: NiFlowPoll) => void; reject: (e: unknown) => void }> = [];
    const fetchFlow = vi.fn((id: string) => new Promise<NiFlowPoll>((resolve, reject) => {
      calls.push(id);
      waiting.push({ id, resolve, reject });
    }));
    const board = createBoard({ fetchBoard: api.fetchBoard, fetchFlow, isUnlocked: () => true });
    cleanups.push(() => board.stop());
    board.start();
    void board.load();
    api.respond(0, initial);
    return { api, board, fetchFlow, waiting, calls };
  }

  it("polls only the active v2 item every 3 s, never the whole board", async () => {
    const { api, board, fetchFlow, calls } = setup([v2("a", 1, "sampling"), v2("b", 2)]);
    await settle();
    expect(board.anyActive).toBe(true);
    await vi.advanceTimersByTimeAsync(3_000);
    expect(calls).toEqual(["a"]);
    expect(fetchFlow).toHaveBeenCalledTimes(1);
    expect(api.fetchBoard).toHaveBeenCalledTimes(1); // only the initial load
  });

  it("merges the answer into the row without touching the others", async () => {
    const { board, waiting } = setup([v2("a", 1, "sampling"), v2("b", 2)]);
    await settle();
    await vi.advanceTimersByTimeAsync(3_000);
    waiting[0].resolve(poll("a", "mapping"));
    await settle();
    expect(board.items[0].flow?.state).toBe("mapping");
    expect(board.items[1].flow).toBeNull();
    expect(board.items.map((i) => i.id)).toEqual(["a", "b"]);
  });

  it("stops polling once the flow is terminal and reloads the board once", async () => {
    const { api, board, fetchFlow, waiting } = setup([v2("a", 1, "sampling")]);
    await settle();
    await vi.advanceTimersByTimeAsync(3_000);
    waiting[0].resolve(poll("a", "failed"));
    await settle();
    expect(board.anyActive).toBe(false);
    expect(api.fetchBoard).toHaveBeenCalledTimes(2); // initial + the one reload
    api.respond(1, [v2("a", 1, "failed")]);
    await settle();
    await vi.advanceTimersByTimeAsync(9_000);
    expect(fetchFlow).toHaveBeenCalledTimes(1);
  });

  it("drops a poll answer that lands after a newer board load", async () => {
    const { api, board, waiting } = setup([v2("a", 1, "sampling")]);
    await settle();
    await vi.advanceTimersByTimeAsync(3_000);
    void board.load(); // a newer load starts while the poll is in flight
    waiting[0].resolve(poll("a", "mapping"));
    await settle();
    expect(board.items[0].flow?.state).toBe("sampling");
    api.respond(1, [v2("a", 1, "assembling")]);
    await settle();
    expect(board.items[0].flow?.state).toBe("assembling");
  });

  it("does not start a second request for an item whose poll is still in flight", async () => {
    const { fetchFlow, waiting } = setup([v2("a", 1, "sampling")]);
    await settle();
    await vi.advanceTimersByTimeAsync(9_000);
    expect(fetchFlow).toHaveBeenCalledTimes(1);
    waiting[0].resolve(poll("a", "sampling"));
    await settle();
    await vi.advanceTimersByTimeAsync(3_000);
    expect(fetchFlow).toHaveBeenCalledTimes(2);
  });

  it("a failed poll is ignored and retried on the next tick", async () => {
    const { board, fetchFlow, waiting } = setup([v2("a", 1, "sampling")]);
    await settle();
    await vi.advanceTimersByTimeAsync(3_000);
    waiting[0].reject(new Error("offline"));
    await settle();
    expect(board.lastError).toBeNull();
    await vi.advanceTimersByTimeAsync(3_000);
    expect(fetchFlow).toHaveBeenCalledTimes(2);
  });
});
