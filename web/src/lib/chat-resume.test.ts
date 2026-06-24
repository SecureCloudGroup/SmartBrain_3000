// Regression #11: leaving /chat and coming back must NOT silently start a new chat.
// The bug: resuming was gated on the conversation-LIST load, so a transient list failure
// left an empty page that looked new. resumeChat fetches the open conversation directly and
// only forgets it on a confirmed 404.

import { describe, expect, it } from "vitest";

import { resumeChat } from "./chat-resume";

const isNotFound = (e: unknown) => (e as { status?: number })?.status === 404;

describe("resumeChat", () => {
  it("returns [] and fetches nothing when no chat is open", async () => {
    let called = false;
    const out = await resumeChat({ currentId: null }, async () => {
      called = true;
      return { messages: ["x"] };
    }, isNotFound);
    expect(out).toEqual([]);
    expect(called).toBe(false);
  });

  it("resumes the open conversation's messages and keeps currentId", async () => {
    const session = { currentId: "c1" };
    const out = await resumeChat(session, async (id) => ({ messages: [`msg-${id}`] }), isNotFound);
    expect(out).toEqual(["msg-c1"]);
    expect(session.currentId).toBe("c1");
  });

  it("drops currentId only on a confirmed 404 (deleted conversation)", async () => {
    const session = { currentId: "gone" };
    const out = await resumeChat(session, async () => {
      throw { status: 404 };
    }, isNotFound);
    expect(out).toEqual([]);
    expect(session.currentId).toBeNull();
  });

  it("KEEPS currentId on a transient error and rethrows (the core of the regression)", async () => {
    const session = { currentId: "c2" };
    await expect(
      resumeChat(session, async () => {
        throw { status: 503 };
      }, isNotFound),
    ).rejects.toBeDefined();
    expect(session.currentId).toBe("c2"); // a transient failure must NOT look like a new chat
  });
});
