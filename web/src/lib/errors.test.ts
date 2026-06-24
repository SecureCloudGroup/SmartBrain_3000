// Centralizes the error-string mapping users see. The contract: 423 -> "" (so callers
// don't paint anything before api.ts navigates to /unlock), 502/503/504 -> the "couldn't
// reach the model" sentence, other 5xx -> "something went wrong", 4xx -> the backend
// detail (already human-phrased), non-ApiError -> the generic network message.

import { describe, expect, it } from "vitest";

import { ApiError } from "./api";
import { describeError } from "./errors";

describe("describeError", () => {
  it("returns empty string for 423 so callers don't flash an error before /unlock", () => {
    expect(describeError(new ApiError(423, "locked"))).toBe("");
  });

  it("maps 502/503/504 to the 'couldn't reach the model' sentence", () => {
    const msg = "I couldn't reach the model just now — try again in a moment.";
    expect(describeError(new ApiError(502, "bad gateway"))).toBe(msg);
    expect(describeError(new ApiError(503, "unavailable"))).toBe(msg);
    expect(describeError(new ApiError(504, "timeout"))).toBe(msg);
  });

  it("maps other 5xx to the generic 'something went wrong' sentence", () => {
    expect(describeError(new ApiError(500, "boom"))).toBe(
      "Something went wrong on my end — please try again.",
    );
  });

  it("passes the backend detail through on 4xx (already user-phrased)", () => {
    expect(describeError(new ApiError(400, "title is required"))).toBe("title is required");
    expect(describeError(new ApiError(404, "no such conversation"))).toBe("no such conversation");
  });

  it("falls back to a friendly 4xx default when the detail is empty", () => {
    // ApiError's message can't be "" if you construct with one — guard against the
    // theoretical case where err.message is falsy on 4xx.
    const e = new ApiError(400, "");
    expect(describeError(e)).toBe("That didn't work — please try again.");
  });

  it("returns the network message for any non-ApiError (TypeError, string, undefined)", () => {
    const net = "Couldn't reach SmartBrain — check your connection and try again.";
    expect(describeError(new TypeError("Failed to fetch"))).toBe(net);
    expect(describeError("nope")).toBe(net);
    expect(describeError(undefined)).toBe(net);
  });
});
