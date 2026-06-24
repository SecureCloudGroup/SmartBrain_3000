import { ApiError } from "./api";

// Turn any thrown error into a plain-language, user-facing message.
//
// A 423 (vault locked) returns "" — api.ts has already redirected to /unlock, so
// callers should NOT paint an error for it (avoids a flash before navigation).
//
// For server/transport failures (5xx, timeout, network) we return a human sentence
// rather than the raw backend `detail` (often an exception string). Client errors
// (4xx) carry messages the API already phrases for a person, so those pass through.
export function describeError(err: unknown): string {
  if (!(err instanceof ApiError)) {
    // Thrown before/without a response — almost always a network/connection drop.
    return "Couldn't reach SmartBrain — check your connection and try again.";
  }
  const s = err.status;
  if (s === 423) return ""; // locked: api.ts already navigated to /unlock
  if (s === 502 || s === 503 || s === 504) {
    return "I couldn't reach the model just now — try again in a moment.";
  }
  if (s >= 500) return "Something went wrong on my end — please try again.";
  // 4xx: the backend detail is already human-readable (validation, not-found, etc.).
  return err.message || "That didn't work — please try again.";
}
