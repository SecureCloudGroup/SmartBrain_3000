// Pure helpers behind the page-browser surfaces of the Neural Interface board (Round 20, B3b;
// ni-format §35 "Client (B3b)"). Every sentence here is code-authored. The only server data the
// board shows from these fields are the sealed sites and feed addresses, as plain text, and the
// server's closed refusal sentences. A board row or flow field from an older server lacks the
// fields, and every helper then answers "nothing to show" (the version-skew rule).

import type { NiBoardItem, NiFeedCandidate, NiItemFlow } from "$lib/api";

// The install phases that mean "getting ready" (browsers/install.py TRANSIENT).
const TRANSIENT_PHASES: ReadonlySet<string> = new Set(["downloading", "verifying", "unpacking", "self-testing"]);
// A card names at most 12 sites (ni.MAX_RENDER_SITES) and offers at most 5 feeds
// (ni.MAX_FEED_CANDIDATES).
const MAX_SITES = 12;
const MAX_FEEDS = 5;
// The fixed part of the run route's 409 while the page browser is busy (ni_routes.run_item) —
// the fallback for a server that sends no `code`.
const BUSY_DETAIL = "page browser is busy";

/** The card's state line while the page browser can't run on this machine — never a failure. */
export const RENDER_UNAVAILABLE =
  "The page browser is unavailable on this machine, so this card can't update here. " +
  "This isn't a failure — Settings → Status shows why.";

/** The transient note for a run the page browser was too busy to take. */
export const RENDER_BUSY = "The page browser is busy — try again in a moment.";

/** "Preparing the page browser (43%)…" while a build waits for the page browser's first
 *  install or check (`flow.browser` in a transient phase), else "". */
export function browserWaitLabel(flow: NiItemFlow | null | undefined): string {
  console.assert(flow === null || flow === undefined || typeof flow === "object", "browserWaitLabel: flow shape");
  const wait = flow?.browser;
  if (!wait || typeof wait !== "object" || !TRANSIENT_PHASES.has(wait.phase)) return "";
  const pct = Number.isFinite(wait.pct) ? Math.min(100, Math.max(0, Math.round(wait.pct))) : 0;
  console.assert(pct >= 0 && pct <= 100, "browserWaitLabel: pct clamped");
  return `Preparing the page browser (${pct}%)…`;
}

/** The paste consent's closed clause (R6): when a pasted page may be read in the page browser
 *  here, the one visit may also load the sites the page depends on (at most `site_cap`). "" when
 *  the pick pause carries no notice. */
export function tapClause(flow: NiItemFlow | null | undefined): string {
  console.assert(flow === null || flow === undefined || typeof flow === "object", "tapClause: flow shape");
  const tap = flow?.tap;
  if (!tap || typeof tap !== "object" || tap.render_notice !== true) return "";
  const cap = Number.isInteger(tap.site_cap) && tap.site_cap > 0 ? ` (up to ${tap.site_cap})` : "";
  console.assert(cap === "" || cap.startsWith(" (up to "), "tapClause: the cap reads as a parenthesis");
  return `If the page needs SmartBrain's page browser, that one visit may also load the sites it depends on${cap}.`;
}

/** The other sites a draft waiting for its Activate loads (`draft.reason === "render_sites"`):
 *  the sealed list without the page's own site (always first), unique, ≤ 12 — or null when the
 *  card is no such draft (its plain Activate shows then). */
export function renderDraftSites(item: NiBoardItem): string[] | null {
  console.assert(typeof item.id === "string", "renderDraftSites: a board row");
  const draft = item.draft;
  if (!draft || draft.reason !== "render_sites" || !Array.isArray(draft.sites)) return null;
  const others: string[] = [];
  for (const site of draft.sites.slice(1, 1 + MAX_SITES)) { // bounded: ≤ 12 sites
    if (typeof site === "string" && site.trim() !== "" && !others.includes(site)) others.push(site);
  }
  console.assert(others.length <= MAX_SITES, "renderDraftSites: capped");
  return others.length > 0 ? others : null;
}

/** The draft panel's first line: "This page also loads 3 other sites". */
export function alsoLoadsLine(count: number): string {
  console.assert(Number.isInteger(count), "alsoLoadsLine: a whole count");
  const n = Math.max(0, Math.trunc(count));
  console.assert(n >= 0, "alsoLoadsLine: never negative");
  return `This page also loads ${n} other site${n === 1 ? "" : "s"}`;
}

/** The card's state line when the page browser was unavailable on this machine at the card's
 *  last update (`render.unavailable`): no streak, no failing reason. "" otherwise. */
export function renderNotice(item: NiBoardItem): string {
  console.assert(typeof item.id === "string", "renderNotice: a board row");
  const unavailable = item.rendered === true && item.render?.unavailable === true;
  console.assert(typeof unavailable === "boolean", "renderNotice: a verdict");
  return unavailable ? RENDER_UNAVAILABLE : "";
}

/** The site's data feeds the creation visit saw (R12): https addresses only, one row per
 *  address, ≤ 5 — [] when the row carries none (the C2 panel then offers no feed). */
export function feedCandidates(item: NiBoardItem): NiFeedCandidate[] {
  console.assert(typeof item.id === "string", "feedCandidates: a board row");
  const raw = item.preview?.feed_candidates;
  if (!Array.isArray(raw)) return [];
  const out: NiFeedCandidate[] = [];
  for (const feed of raw.slice(0, MAX_FEEDS)) { // bounded: ≤ 5 rows
    const ok = !!feed && typeof feed.url === "string" && feed.url.startsWith("https://")
      && typeof feed.site === "string" && !out.some((seen) => seen.url === feed.url);
    if (ok) out.push({ url: feed.url, site: feed.site });
  }
  console.assert(out.length <= MAX_FEEDS, "feedCandidates: capped");
  return out;
}

/** The page browser's two capacity answers — never failures (ni.CAPACITY_KINDS) — or "". */
export type RenderCapacity = "busy" | "unavailable" | "";

// The server's machine-readable codes for them: beside `detail` on a refused request (where
// api.ts reads `code`) and in a run's or a C2 verdict's reply.
const CAPACITY_CODES: Readonly<Record<string, RenderCapacity>> = {
  render_busy: "busy",
  render_unavailable: "unavailable",
};

/** A server code's capacity answer: "" for any code outside the two (another refusal). */
function capacityOfCode(code: string): RenderCapacity {
  console.assert(code.length > 0, "capacityOfCode: a code");
  const cap = Object.hasOwn(CAPACITY_CODES, code) ? CAPACITY_CODES[code] : "";
  console.assert(cap === "" || cap === "busy" || cap === "unavailable", "capacityOfCode: closed answer");
  return cap;
}

/** A refused request's capacity answer. The server's `code` decides when it sends one
 *  ("render_busy" / "render_unavailable", anything else is no capacity answer); from a server
 *  that sends none, the busy 409's fixed sentence is matched. "" for every other error. */
export function errorCapacity(err: unknown): RenderCapacity {
  console.assert(BUSY_DETAIL.length > 0, "errorCapacity: the fallback sentence");
  if (!(err instanceof Error)) return "";
  const { status, code } = err as { status?: unknown; code?: unknown };
  const cap = typeof code === "string" && code !== "" ? capacityOfCode(code)
    : status === 409 && err.message.includes(BUSY_DETAIL) ? "busy" : "";
  console.assert(cap === "" || cap === "busy" || cap === "unavailable", "errorCapacity: closed answer");
  return cap;
}

/** A run's or a C2 verdict's capacity answer, from its reply. The server's `code` decides when
 *  it sends one; from a server that sends none, the older fields — `run: "busy"` on a C2
 *  reply, `kind: "render_unavailable"` on a run. "" for every other reply. */
export function replyCapacity(reply: { code?: string; run?: string; kind?: string } | null | undefined): RenderCapacity {
  console.assert(reply === null || reply === undefined || typeof reply === "object", "replyCapacity: reply shape");
  if (!reply) return "";
  const code = reply.code;
  const cap = typeof code === "string" && code !== "" ? capacityOfCode(code)
    : reply.run === "busy" ? "busy" : reply.kind === "render_unavailable" ? "unavailable" : "";
  console.assert(cap === "" || cap === "busy" || cap === "unavailable", "replyCapacity: closed answer");
  return cap;
}

/** The transient note for a run the page browser didn't take ("" for no capacity answer). */
export function capacityNote(cap: RenderCapacity): string {
  console.assert(cap === "" || cap === "busy" || cap === "unavailable", "capacityNote: closed input");
  const note = cap === "busy" ? RENDER_BUSY : cap === "unavailable" ? RENDER_UNAVAILABLE : "";
  console.assert(note !== "" || cap === "", "capacityNote: every answer has its note");
  return note;
}

/** The C2 YES toast when the page browser didn't take the verdict's first check ("" for no
 *  capacity answer — the page then says what the check did). */
export function confirmedNote(cap: RenderCapacity): string {
  console.assert(cap === "" || cap === "busy" || cap === "unavailable", "confirmedNote: closed input");
  const note = cap === "busy"
    ? "Confirmed — the page browser is busy, so the card checks its data on its next update."
    : cap === "unavailable"
      ? "Confirmed — the page browser is unavailable on this machine, so the card can't check its data yet. Settings → Status shows why."
      : "";
  console.assert(note !== "" || cap === "", "confirmedNote: every answer has its note");
  return note;
}
