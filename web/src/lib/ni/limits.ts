// What a card says about its source's free request limit and a host's wait (2026-10-31, T: "Prefer a source that
// fits"). The backend writes both from what the Library documents and what the host said (`limit`, `waiting` on the
// board row); this only shows them, and puts the next try in the viewer's own clock.
import type { NiBoardItem } from "$lib/api";

/** The line under Keep when the source's free limit doesn't cover how often the card refreshes ("Alpha Vantage's
 *  free key allows 25 requests a day. Refreshing every 30 minutes needs 48, so this card will stop updating partway
 *  through each day."). "" when it covers it, or no limit is known. */
export function keepLimitLine(item: NiBoardItem): string {
  console.assert(typeof item.id === "string", "keepLimitLine: a board row");
  const line = typeof item.limit === "string" ? item.limit.trim() : "";
  console.assert(line.length <= 400, "keepLimitLine: a bounded line");
  return line;
}

/** The time a wait ends, as the viewer's clock says it ("3:15 PM"); "" for a time that doesn't parse. */
export function tryAgainAt(until: string, locale?: string): string {
  console.assert(typeof until === "string", "tryAgainAt: an ISO time");
  const at = new Date(until);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" });
}

/** The card's note while its source's host holds SmartBrain off: why the card isn't updating and when it next
 *  tries. With a known limit the card names it; without one it gives the host's own words. "" when no wait holds
 *  (a held card waiting for Keep shows its limit under Keep instead). */
export function waitingNotice(item: NiBoardItem, locale?: string): string {
  console.assert(typeof item.id === "string", "waitingNotice: a board row");
  const w = item.waiting;
  if (!w || typeof w.host !== "string" || !w.host || item.awaiting_yes) return "";
  const at = tryAgainAt(w.until, locale);
  const next = at ? ` The card tries again at ${at}.` : " The card tries again later.";
  const limit = keepLimitLine(item);
  if (limit) return `${limit} ${w.host} asked SmartBrain to wait.${next}`;
  const said = typeof w.said === "string" ? w.said.trim() : "";
  return said ? `${w.host} asked SmartBrain to wait: “${said}”${next}` : `${w.host} asked SmartBrain to wait.${next}`;
}
