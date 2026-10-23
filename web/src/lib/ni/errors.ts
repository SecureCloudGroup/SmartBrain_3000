// Human sentences for the host-free error classes NI stamps on failed runs (§6/§8).
// Pure + total: an unknown class reads back verbatim so the card still names the
// truth instead of a lie ("something went wrong"). Kept in one place so the board
// card and the run-history modal render the same friendly text for the same class.

const FRIENDLY: Record<string, string> = {
  extract_miss: "couldn't find the expected data",
  fetch_failed: "couldn't reach the source",
  image_type: "not a supported image",
  llm_requires_local: "needs a local model",
  model_consent_required: "needs your OK to use a model off this computer; rebuild the card to choose",
  mcp_unavailable: "your MCP server didn't answer",
  // S9 (ni-format §23.1): a kept research-built card re-checked its constraints and the source
  // no longer answers the question. The card keeps showing its last good reading.
  constraint_drift: "this source stopped answering your question; the last good reading is still shown",
  // S7 (ni-format §38): a compare card reads one address per thing; when one of them fails the whole
  // card fails loudly rather than showing fewer things than it was kept with.
  compare_partial: "one of the compared things couldn't be read; the last good comparison is still shown",
  secret_missing: "credential problem",
  secret_host_mismatch: "credential problem",
  // Round 20 (B3a, ni-format §35): the page browser's failure classes (ni._RENDER_FAILURES).
  // Its capacity kinds (render_busy, render_unavailable) are never failures and have no entry
  // here — the card says so with its own state line.
  render_refused: "the page browser wouldn't open this address",
  render_timeout: "the page took too long in the page browser",
  render_blocked: "the site turned the page browser away",
  render_offsite: "the page moved to a site this card doesn't allow",
  render_confinement: "the page browser's safety checks stopped the read",
  render_oversize: "the page was too big for the page browser",
  render_engine: "the page browser couldn't read the page",
};

/** Map a host-free error class to a short, calm sentence. Unknown classes echo
 *  the raw class so the card never invents a story it doesn't know. */
export function friendlyErrorClass(cls: string | null | undefined): string {
  console.assert(
    cls === null || cls === undefined || typeof cls === "string",
    "friendlyErrorClass: cls is string|null|undefined",
  );
  console.assert(typeof FRIENDLY === "object", "friendlyErrorClass: map present");
  const key = typeof cls === "string" ? cls.trim() : "";
  if (!key) return "";
  return FRIENDLY[key] ?? key;
}
