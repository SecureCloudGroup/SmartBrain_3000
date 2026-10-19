// Regression #11: leaving /chat and coming back must NOT silently start a new chat.
// The bug: resuming was gated on the conversation-LIST load, so a transient list failure
// left an empty page that looked new. resumeChat fetches the open conversation directly and
// only forgets it on a confirmed 404.

import { describe, expect, it } from "vitest";

import { IN_FLIGHT_MAX_RETRIES, IN_FLIGHT_RETRY_MS, inFlightRetryDelay, resumeChat } from "./chat-resume";

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

describe("inFlightRetryDelay", () => {
  const running = { status: "awaiting_approval", in_flight: true, pending: [] };

  it("retries while an approved tool is still running and nothing awaits the user", () => {
    expect(inFlightRetryDelay(running, 0)).toBe(IN_FLIGHT_RETRY_MS);
    expect(inFlightRetryDelay(running, IN_FLIGHT_MAX_RETRIES - 1)).toBe(IN_FLIGHT_RETRY_MS);
  });

  it("stops once the retry budget (~2 minutes) is spent", () => {
    expect(inFlightRetryDelay(running, IN_FLIGHT_MAX_RETRIES)).toBeNull();
    expect(IN_FLIGHT_RETRY_MS * IN_FLIGHT_MAX_RETRIES).toBe(120_000);
  });

  it("never retries when the user must decide, or the turn finished", () => {
    const card = [{ id: "p1" }];
    expect(inFlightRetryDelay({ status: "awaiting_approval", in_flight: true, pending: card }, 0)).toBeNull();
    expect(inFlightRetryDelay({ status: "awaiting_approval", pending: [] }, 0)).toBeNull();
    expect(inFlightRetryDelay({ status: "complete", in_flight: true }, 0)).toBeNull();
  });
});
