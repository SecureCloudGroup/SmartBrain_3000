// Shared helpers for rendering a pending approval (Activity's cards and Chat's inline
// cards must read identically — same icon, same args formatting).
import type { IconName } from "$lib/icons";

// A rough tool->icon mapping so pending cards read at a glance; pencil is the
// honest default for "changes something".
export function iconForTool(tool: string): IconName {
  const t = tool.toLowerCase();
  if (t.includes("mail") || t.includes("email")) return "mail";
  if (t.includes("task")) return "tasks";
  if (t.includes("schedule")) return "clock";
  if (t.includes("kb") || t.includes("knowledge") || t.includes("note") || t.includes("doc")) return "book";
  if (t.includes("web") || t.includes("fetch") || t.includes("search")) return "search";
  if (t.includes("vault")) return "vault";
  return "pencil";
}

// Show tool args as readable "key: value" lines instead of raw JSON. Accepts an
// object (pending tiles) or a JSON string (history args_summary, already
// redacted + capped server-side); long values are truncated for display.
export function fmtArgs(args: unknown): string {
  let obj: unknown = args;
  if (typeof args === "string") {
    if (!args.trim()) return "";
    try {
      obj = JSON.parse(args);
    } catch {
      return args; // truncated / non-JSON summary — show as-is
    }
  }
  if (obj && typeof obj === "object" && !Array.isArray(obj)) {
    return Object.entries(obj as Record<string, unknown>)
      .map(([k, v]) => {
        const s = typeof v === "string" ? v : JSON.stringify(v);
        return `${k}: ${s.length > 200 ? s.slice(0, 200) + "…" : s}`;
      })
      .join("\n");
  }
  return typeof args === "string" ? args : JSON.stringify(args);
}
