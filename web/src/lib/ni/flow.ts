// Friendly stage labels for the Natural-Interface creation/remap flow. Pure + total:
// an unknown state reads back "" so the card falls through to the next honest line
// instead of rendering a lie ("something's happening…"). Kept in one place so the
// board card and any future surface (e.g. the chat inline card) render the same copy.
//
// Design note (§ Status truth): the "source" state is overloaded — the engine may
// be autonomously searching OR waiting for the user to pick between candidates in
// chat. The backend signals the pick-wait case by setting `flow.error` on a source
// state. Every other state has a 1:1 label.

import type { NiAwaitingYes, NiItemFlow, NiSuggestion } from "$lib/api";
import { browserWaitLabel } from "$lib/ni/browser";
import { ACTION_KINDS } from "$lib/ni/types.gen";

/** The flow states in which nothing is building: the flow is over (a fault, an answerable stop, or the honest
 *  decline). Mirrors ni_master.TERMINAL_STATES; a state this bundle does not know is never terminal. */
export const TERMINAL_FLOW_STATES: ReadonlySet<string> = new Set(["failed", "unsupported", "declined"]);

/** What a declined card says first when the backend's own headline is missing (it always sends one). */
export const DECLINED_HEADLINE = "I couldn't build a reliable card for this";

// Marker the backend sets on flow.error when a `source` state is parked pending the
// user picking between candidate sources (a resume_ni_flow approval will be parked
// on the pending list). Kept as a constant so a rename shows up at every call site.
export const AWAITING_SOURCE_PICK = "awaiting_pick";

/** Return the calm, human sentence for this flow's current stage, or "" for the
 *  terminal error states (unsupported/failed/declined — the caller composes those with
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
  // Round 20 (B3a): a build waiting for the page browser's first install or check says so,
  // with its percentage, in place of its stage.
  const wait = TERMINAL_FLOW_STATES.has(s) ? "" : browserWaitLabel(flow);
  if (wait) return wait;
  // v2: the backend's own progress label (terminal flows carry none and fall through).
  if (flow.progress?.label && !TERMINAL_FLOW_STATES.has(s)) return flow.progress.label;
  if (s === "intent") return "Understanding your request…";
  if (s === "source") {
    return flow.error === AWAITING_SOURCE_PICK
      ? "Waiting for you to pick a source on the card"
      : "Finding the source…";
  }
  if (s === "awaiting_access") return "Needs your key or contact email";
  if (s === "sampling") return "Reading a sample…";
  if (s === "mapping") return "Choosing the data fields…";
  if (s === "assembling") return "Building the card…";
  if (s === "awaiting_credential") return "Needs your API key";
  if (s === "awaiting_params") return "Needs a detail from you";
  if (s === "ready") return "Building the card…";
  // failed / unsupported: caller renders friendlyErrorClass(flow.error); declined: the DeclinedCard.
  return "";
}

/** The action kinds this client knows how to render: ni_master.ACTION_KINDS as generated when this
 *  bundle was built. It matches the backend today; the filter only matters when a newer server
 *  sends a kind this bundle predates, which is ignored, never shown. */
export const KNOWN_ACTIONS: ReadonlySet<string> = new Set<string>(ACTION_KINDS);

/** The backend-declared actions of a board row, unknown kinds dropped; `null` for a row without an
 *  `actions` key (none is offered). A flow's own list wins over the row's while it is active. */
export function rowActions(row: { actions?: string[]; flow?: { actions?: string[] } | null } | null | undefined): string[] | null {
  console.assert(row === null || row === undefined || typeof row === "object", "rowActions: row is object");
  const list = row?.flow?.actions ?? row?.actions;
  console.assert(list === undefined || Array.isArray(list), "rowActions: actions is an array when present");
  if (!Array.isArray(list)) return null;
  return list.filter((k) => typeof k === "string" && KNOWN_ACTIONS.has(k));
}

/** True when the row's backend-declared actions include `kind`. False for a row without actions and
 *  for an unknown kind. */
export function hasAction(
  row: { actions?: string[]; flow?: { actions?: string[] } | null } | null | undefined,
  kind: string,
): boolean {
  console.assert(typeof kind === "string" && kind !== "", "hasAction: kind required");
  console.assert(row === null || row === undefined || typeof row === "object", "hasAction: row is object");
  return rowActions(row)?.includes(kind) ?? false;
}

/** The card menu's pause/resume button: "pause" or "resume", or "" when the row offers neither. */
export function pauseKind(
  row: { actions?: string[]; flow?: { actions?: string[] } | null },
): "pause" | "resume" | "" {
  console.assert(typeof row === "object" && row !== null, "pauseKind: row is object");
  const list = rowActions(row);
  console.assert(list === null || Array.isArray(list), "pauseKind: list is array or null");
  if (list === null) return "";
  return list.includes("pause") ? "pause" : list.includes("resume") ? "resume" : "";
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
  return !TERMINAL_FLOW_STATES.has(flow.state);
}

/** Where a card waiting for the user's YES got its reading (ruling 2026-10-04), as the card
 *  says it: "From the web page <host> — <title>" / "From the dataset <host> — <title>". The
 *  title is left off when the source had none (a pasted link) or it only repeats the host. */
export function awaitingYesSource(a: NiAwaitingYes): string {
  console.assert(typeof a === "object" && a !== null, "awaitingYesSource: a is object");
  console.assert(typeof a.host === "string" && typeof a.title === "string", "awaitingYesSource: strings");
  const host = a.host.trim();
  const title = a.title.trim();
  if (a.from === "library") {
    // v2 auto-pick: an installed Library source the engine chose and built from.
    const named = title !== "" && title.toLowerCase() !== host.toLowerCase();
    const name = named ? title : host;
    if (!name) return "From the SmartBrain Library";
    return host && named ? `From the SmartBrain Library: ${title} (${host})` : `From the SmartBrain Library: ${name}`;
  }
  const kind = a.from === "page" ? "web page" : "dataset";
  const named = title !== "" && title.toLowerCase() !== host.toLowerCase();
  if (!host) return named ? `From the ${kind} “${title}”` : `From a ${kind}`;
  return named ? `From the ${kind} ${host} — ${title}` : `From the ${kind} at ${host}`;
}

/** The consent line above Keep on an auto-picked Library card: the one read already made, and
 *  that no further read happens until Keep. "" for page and dataset cards (their tap was the
 *  consent). Mentions the typed key only when the backend says the build used one. */
export function awaitingYesNotice(a: NiAwaitingYes): string {
  console.assert(typeof a === "object" && a !== null, "awaitingYesNotice: a is object");
  console.assert(typeof a.from === "string", "awaitingYesNotice: from is string");
  if (a.from !== "library") return "";
  const base = "SmartBrain read this source once to build this preview. It won’t read it again until you tap Keep.";
  return a.used_key === true ? `${base} It used the key you typed.` : base;
}

/** The two buttons of the awaiting-yes block. A card the v2 auto-pick built from the Library
 *  reads "Keep" / "Try another source"; every other card keeps its YES / NO wording. */
export function awaitingYesButtons(a: NiAwaitingYes): { yes: string; yesBusy: string; no: string } {
  console.assert(typeof a === "object" && a !== null, "awaitingYesButtons: a is object");
  console.assert(typeof a.from === "string", "awaitingYesButtons: from is string");
  if (a.from === "library") return { yes: "Keep", yesBusy: "Keeping…", no: "Try another source" };
  return { yes: "Yes, that’s it", yesBusy: "Checking…", no: "No, try another source" };
}

/** The source-pick card's rows, split the way the card shows them (ruling 2026-10-05: "Library
 *  cards; web as links"): `sources` are Library sources that declare their answers — a tap builds
 *  the card; `links` are pages that may help (a Library dataset without declared answers, a web
 *  page the search found) — they open in a new tab and never build. `heading` is the card's first
 *  line. An unknown kind is never offered as a tap. */
export function pickRows(suggestions: NiSuggestion[] | undefined): {
  sources: NiSuggestion[];
  links: NiSuggestion[];
  heading: string;
} {
  console.assert(suggestions === undefined || Array.isArray(suggestions), "pickRows: array or undefined");
  const rows = suggestions ?? [];
  const seen = new Set<string>(); // one row per address: the card keys its rows on url
  const once = (s: NiSuggestion): boolean => {
    if (seen.has(s.url)) return false;
    seen.add(s.url);
    return true;
  };
  const sources = rows.filter((s) => s.kind === "library" && once(s));
  const links = rows.filter((s) => s.kind === "link" && /^https:\/\//.test(s.url) && once(s));
  const heading = sources.length > 0
    ? "From the SmartBrain Library — tap the one that fits:"
    : links.length > 0
      ? "No SmartBrain Library source answers this yet."
      : "No SmartBrain Library source answers this yet — paste a link to the data:";
  return { sources, links, heading };
}

/** The line under a link: where it is and what it is ("data.ny.gov · dataset"). */
export function linkWhat(link: NiSuggestion): string {
  console.assert(typeof link.host === "string", "linkWhat: host is string");
  const what = link.found === "dataset" ? "dataset" : "web page";
  return link.host.trim() ? `${link.host.trim()} · ${what}` : what;
}

/** The links a declined card shows: https only, one per address, at most three (the backend already cleans
 *  them; this is the client's own guard, so a link it would not open is never rendered). */
export function declinedLinks(decline: { links?: Array<{ url: string; title: string; host: string }> } | null | undefined):
  Array<{ url: string; title: string; host: string }> {
  console.assert(decline === null || decline === undefined || typeof decline === "object", "declinedLinks: object");
  const seen = new Set<string>();
  const out: Array<{ url: string; title: string; host: string }> = [];
  for (const link of decline?.links ?? []) {
    if (typeof link?.url !== "string" || !/^https:\/\//.test(link.url) || seen.has(link.url)) continue;
    seen.add(link.url);
    out.push(link);
    if (out.length === 3) break;
  }
  console.assert(out.length <= 3, "declinedLinks: at most three");
  return out;
}
