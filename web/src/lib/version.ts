// Format the backend's version (from /api/health) for display under the logo. An empty or missing
// version — health unreachable, or a blank string — renders nothing, never a bare "v"/"vundefined".
export function displayVersion(v?: string | null): string {
  return v ? `v${v}` : "";
}
