// Shared helpers for rendering a pending approval (Activity's cards and Chat's inline
// cards must read identically — same icon, same args formatting).
import type { IconName } from "$lib/icons";

// A rough tool->icon mapping so pending cards read at a glance; pencil is the
// honest default for "changes something".
export function iconForTool(tool: string): IconName {
  const t = tool.toLowerCase();
  if (t.includes("ni_item")) return "monitor";
  if (t.includes("mail") || t.includes("email")) return "mail";
  if (t.includes("task")) return "tasks";
  if (t.includes("schedule")) return "clock";
  if (t.includes("kb") || t.includes("knowledge") || t.includes("note") || t.includes("doc")) return "book";
  if (t.includes("web") || t.includes("fetch") || t.includes("search")) return "search";
  if (t.includes("vault")) return "vault";
  return "pencil";
}

// Consent law: the source a create_ni_item / update_ni_item card would call must be
// UNMISSABLE — fmtArgs buries it mid-JSON otherwise. Returns a promoted line for those
// tools when the args carry an identifiable source, else null:
//   http_json / http_page  → "Fetches: <url>"
//   mcp_tool (§22)         → "MCP: <server label> → <tool>" (falls back to a generic
//                             phrase when the caller didn't resolve a label yet)
// Handles both an object (pending tiles) and a JSON string (history args_summary). A
// params-substituted URL simply renders the template — that's fine; the template still
// names the host. `arguments` themselves already render whole via fmtArgs.
export function promotedLine(tool: string, args: unknown, mcpLabel?: string): string | null {
  console.assert(typeof tool === "string", "promotedLine: tool is string");
  console.assert(args !== undefined, "promotedLine: args defined");
  const t = tool.toLowerCase();
  if (t !== "create_ni_item" && t !== "update_ni_item") return null;
  let obj: unknown = args;
  if (typeof args === "string") {
    if (!args.trim()) return null;
    try {
      obj = JSON.parse(args);
    } catch {
      return null;
    }
  }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return null;
  const source = (obj as Record<string, unknown>).source;
  if (!source || typeof source !== "object" || Array.isArray(source)) return null;
  const src = source as Record<string, unknown>;
  const url = src.url;
  if (typeof url === "string" && url.length > 0) return `Fetches: ${url}`;
  // mcp_tool: name the SERVER (the backend attaches its human label to the pending
  // row) so the consent surface reads at a glance. Fallback keeps the card honest
  // when a caller hasn't threaded the label through yet.
  const serverId = src.server_id;
  const toolName = src.tool;
  if (typeof serverId === "string" && serverId.length > 0
      && typeof toolName === "string" && toolName.length > 0) {
    return `MCP: ${mcpLabel ?? "your configured server"} → ${toolName}`;
  }
  return null;
}

// Show tool args as readable "key: value" lines instead of raw JSON. Accepts an
// object (pending tiles) or a JSON string (history args_summary, already
// redacted + capped server-side). Values are shown WHOLE: an approval card that hides the tail of
// an email body or a schedule prompt is exactly where an injected instruction would hide.
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
        return `${k}: ${s}`;
      })
      .join("\n");
  }
  return typeof args === "string" ? args : JSON.stringify(args);
}
