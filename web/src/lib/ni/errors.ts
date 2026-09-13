// Human sentences for the host-free error classes NI stamps on failed runs (§6/§8).
// Pure + total: an unknown class reads back verbatim so the card still names the
// truth instead of a lie ("something went wrong"). Kept in one place so the board
// card and the run-history modal render the same friendly text for the same class.

const FRIENDLY: Record<string, string> = {
  extract_miss: "couldn't find the expected data",
  fetch_failed: "couldn't reach the source",
  image_type: "not a supported image",
  llm_requires_local: "needs a local model",
  mcp_unavailable: "your MCP server didn't answer",
  secret_missing: "credential problem",
  secret_host_mismatch: "credential problem",
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
