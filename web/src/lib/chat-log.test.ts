// Regenerate semantics: WHICH answer gets the control (only the thread's final one)
// and WHAT transcript a regenerated turn sends (history up to and including the last
// user message — the previous answer must not be in it, or the model just agrees with
// itself; errored bubbles and schedule notices were never part of the saved thread).

import { describe, expect, it } from "vitest";

import { finalAssistantId, mergeRefreshedLog, transcriptUpToLastUser, type LogEntry } from "./chat-log";

const user = (id: string, content = "hi"): LogEntry => ({ id, role: "user", content });
const asst = (id: string, content = "hello"): LogEntry => ({ id, role: "assistant", content });
const errBubble = (id: string): LogEntry => ({ id, role: "assistant", content: "boom", err: true });
const sched = (id: string): LogEntry => ({ id, role: "assistant", content: "### Scheduled…", schedule: true });

describe("finalAssistantId", () => {
  it("is null on an empty log", () => {
    expect(finalAssistantId([])).toBeNull();
  });

  it("is the last entry's id when the thread ends on an assistant answer", () => {
    expect(finalAssistantId([user("u1"), asst("a1"), user("u2"), asst("a2")])).toBe("a2");
  });

  it("is null when the thread ends on a user message (nothing to regenerate yet)", () => {
    expect(finalAssistantId([user("u1"), asst("a1"), user("u2")])).toBeNull();
  });

  it("skips trailing schedule notices — they don't end the thread", () => {
    expect(finalAssistantId([user("u1"), asst("a1"), sched("s1"), sched("s2")])).toBe("a1");
  });

  it("is null when the thread ends on an error bubble (that was never a real answer)", () => {
    expect(finalAssistantId([user("u1"), errBubble("e1")])).toBeNull();
  });
});

describe("transcriptUpToLastUser", () => {
  it("is null when there is no user message to regenerate from", () => {
    expect(transcriptUpToLastUser([])).toBeNull();
    expect(transcriptUpToLastUser([sched("s1")])).toBeNull();
  });

  it("cuts AFTER the last user message — the old answer is excluded", () => {
    const out = transcriptUpToLastUser([user("u1", "q1"), asst("a1", "ans1"), user("u2", "q2"), asst("a2", "ans2")]);
    expect(out).toEqual([
      { role: "user", content: "q1" },
      { role: "assistant", content: "ans1" },
      { role: "user", content: "q2" },
    ]);
  });

  it("drops errored bubbles and schedule notices (never persisted server-side)", () => {
    const out = transcriptUpToLastUser([user("u1", "q1"), errBubble("e1"), sched("s1"), user("u2", "q2"), asst("a2")]);
    expect(out).toEqual([
      { role: "user", content: "q1" },
      { role: "user", content: "q2" },
    ]);
  });

  it("returns bare role/content pairs (no client-side ids or flags leak to the API)", () => {
    const out = transcriptUpToLastUser([user("u1", "q")]);
    expect(out).toEqual([{ role: "user", content: "q" }]);
    expect(Object.keys(out![0]).sort()).toEqual(["content", "role"]);
  });
});

describe("mergeRefreshedLog", () => {
  const server: LogEntry[] = [
    { id: "m1", role: "user", content: "hi" },
    { id: "m2", role: "assistant", content: "hello" },
  ];

  it("takes the server thread and re-appends local schedule notices in order", () => {
    const current: LogEntry[] = [
      { id: "old", role: "assistant", content: "stale" },
      { id: "s1", role: "assistant", content: "run A", schedule: true },
      { id: "s2", role: "assistant", content: "run B", schedule: true },
    ];
    expect(mergeRefreshedLog(server, current).map((e) => e.id)).toEqual(["m1", "m2", "s1", "s2"]);
  });

  it("drops unpersisted error bubbles, exactly like a full reload would", () => {
    const current: LogEntry[] = [{ id: "e1", role: "assistant", content: "boom", err: true }];
    expect(mergeRefreshedLog(server, current).map((e) => e.id)).toEqual(["m1", "m2"]);
  });

  it("handles empty inputs", () => {
    expect(mergeRefreshedLog([], [])).toEqual([]);
    expect(mergeRefreshedLog(server, [])).toEqual(server);
  });
});
