// Friendly stage labels for the Natural-Interface creation/remap flow. Pure + total:
// an unknown state reads back "" so the card falls through to the next honest line
// instead of rendering a lie ("something's happening…"). Kept in one place so the
// board card and any future surface (e.g. the chat inline card) render the same copy.
//
// Design note (§ Status truth): the "source" state is overloaded — the engine may
// be autonomously searching OR waiting for the user to pick between candidates in
// chat. The backend signals the pick-wait case by setting `flow.error` on a source
// state. Every other state has a 1:1 label.

import type { NiItemFlow } from "$lib/api";

// Marker the backend sets on flow.error when a `source` state is parked pending the
// user picking between candidate sources (a resume_ni_flow approval will be parked
// on the pending list). Kept as a constant so a rename shows up at every call site.
export const AWAITING_SOURCE_PICK = "awaiting_pick";
// C3 (audit 2026-09-13): a new `confirm_source` state pauses a recipe-matched flow
// until the operator approves the recipe's url_template via `confirm_ni_flow_source`.
// The backend seals `error: "awaiting_confirm"` on that record; the label reads as
// a distinct sentence so a reviewer sees the difference from a pick-a-source pause.
export const AWAITING_SOURCE_CONFIRM = "awaiting_confirm";

/** Return the calm, human sentence for this flow's current stage, or "" for the
 *  terminal error states (unsupported/failed — the caller composes those with
 *  friendlyErrorClass). Returns "" for a null flow too so the card body falls
 *  through to its existing payload / first-run rendering. */
export function flowStageLabel(flow: NiItemFlow | null | undefined): string {
  console.assert(
    flow === null || flow === undefined || typeof flow === "object",
    "flowStageLabel: flow is object|null|undefined",
  );
  console.assert(
    flow === null || flow === undefined || typeof flow.state === "string",
    "flowStageLabel: state is string when flow present",
  );
  if (!flow) return "";
  const s = flow.state;
  if (s === "intent") return "Understanding your request…";
  if (s === "source") {
    return flow.error === AWAITING_SOURCE_PICK
      ? "Waiting for you to pick a source in chat"
      : "Finding the source…";
  }
  if (s === "confirm_source") return "Waiting for you to approve the source";
  if (s === "sampling") return "Reading a sample…";
  if (s === "mapping") return "Choosing the data fields…";
  if (s === "assembling") return "Building the card…";
  if (s === "awaiting_credential") return "Needs your API key";
  if (s === "ready") return "Building the card…";
  // failed / unsupported: caller renders friendlyErrorClass(flow.error).
  return "";
}

/** A flow is "in progress" (fast-poll worthy) when it's non-null and NOT in a
 *  terminal error state. awaiting_credential is user-gated but still counts —
 *  a 3s poll is cheap and picks up the transition the moment the key lands. */
export function isFlowActive(flow: NiItemFlow | null | undefined): boolean {
  console.assert(
    flow === null || flow === undefined || typeof flow === "object",
    "isFlowActive: flow is object|null|undefined",
  );
  console.assert(
    flow === null || flow === undefined || typeof flow.state === "string",
    "isFlowActive: state is string when flow present",
  );
  if (!flow) return false;
  return flow.state !== "failed" && flow.state !== "unsupported";
}
