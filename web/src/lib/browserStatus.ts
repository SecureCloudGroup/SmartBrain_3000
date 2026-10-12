// Plain words behind Settings → Status "Page browser" (Round 20, B3b; ni-format §35). Closed
// tables: every phase, reason code and sandbox kind the server sends (browsers/install.py
// PHASES, browsers.REASONS, walls KINDS) has its own words here; anything else — a newer
// server — reads as a neutral sentence instead of an invented one.

import type { BrowserEngineStatus, BrowsersFlow } from "$lib/api";
import { parseTs } from "$lib/runs";

type ChipKind = "" | "accent" | "ok" | "warn" | "danger";
type ChipWords = { kind: ChipKind; label: string };

/** Why an engine can or cannot read pages here, by the closed reason code. */
export const REASON_SENTENCES: Readonly<Record<string, string>> = {
  disabled: "Switched off on this computer (SMARTBRAIN_NO_BROWSER).",
  manifest: "This version's description of the page browser is damaged — reinstall SmartBrain.",
  platform: "No page browser runs on this operating system; cards read pages without it.",
  missing_libs: "This computer's system libraries are older than the page browser needs.",
  sandbox: "SmartBrain could not build or verify its sandbox here, so the page browser stays off.",
  userns_blocked: "This system blocks the Linux user namespaces the sandbox needs (Docker Desktop does by default).",
  apparmor_userns: "Ubuntu's AppArmor setting blocks the user namespaces the sandbox needs.",
  docker_no_sidecar: "In Docker the page browser runs in its own renderer container, which isn't running.",
  tier_disabled: "Included with this version, not used yet.",
  absent: "Downloads the first time a card needs it.",
  stale: "A newer version downloads the first time a card needs it.",
  preparing: "Getting ready for a card that needs it.",
  error: "The last download or check failed; it tries again the next time a card needs it.",
  self_test: "Checks its sandbox the first time a card needs it after SmartBrain starts.",
};
/** An engine that passed its sandbox check in this app run. */
export const READY_SENTENCE = "Ready — it passed its sandbox check on this computer.";
/** A reason or phase outside the tables (a newer server). */
export const UNKNOWN_SENTENCE = "Not available right now.";

// A row without a reason code says why through its phase (browsers.available's own mapping).
const PHASE_REASON: Readonly<Record<string, string>> = {
  absent: "absent", stale: "stale", error: "error", ready: "self_test", disabled: "disabled",
  unavailable: "sandbox", downloading: "preparing", verifying: "preparing", unpacking: "preparing",
  "self-testing": "preparing",
};
// The one chip an engine row wears, by phase (an eligible engine is "ready" whatever the phase).
const PHASE_CHIPS: Readonly<Record<string, ChipWords>> = {
  downloading: { kind: "accent", label: "downloading" },
  verifying: { kind: "accent", label: "verifying" },
  unpacking: { kind: "accent", label: "unpacking" },
  "self-testing": { kind: "accent", label: "checking" },
  ready: { kind: "", label: "not checked yet" },
  absent: { kind: "", label: "not downloaded" },
  stale: { kind: "", label: "update waiting" },
  unavailable: { kind: "warn", label: "unavailable" },
  error: { kind: "danger", label: "failed" },
  disabled: { kind: "", label: "off" },
};
// The wall the engine runs inside (walls KINDS minus "none").
const SANDBOX_WORDS: Readonly<Record<string, string>> = {
  seatbelt: "the macOS sandbox",
  userns: "Linux namespaces",
  sidecar: "the renderer container",
  null: "the test sandbox",
};

function wordsFor<T>(table: Readonly<Record<string, T>>, key: unknown): T | undefined {
  console.assert(table !== null && typeof table === "object", "wordsFor: a table");
  const hit = typeof key === "string" && Object.hasOwn(table, key) ? table[key] : undefined;
  console.assert(hit === undefined || typeof key === "string", "wordsFor: only string keys hit");
  return hit;
}

/** True while the engine downloads, unpacks or checks itself (the row shows its progress). */
export function engineInMotion(row: BrowserEngineStatus): boolean {
  console.assert(typeof row.phase === "string", "engineInMotion: a phase");
  const moving = ["downloading", "verifying", "unpacking", "self-testing"].includes(row.phase);
  console.assert(typeof moving === "boolean", "engineInMotion: a verdict");
  return moving;
}

/** The engine row's one chip: its phase in plain words ("downloading 43%", "ready", "failed"). */
export function engineChip(row: BrowserEngineStatus): ChipWords {
  console.assert(typeof row.eligible === "boolean", "engineChip: eligible is boolean");
  if (row.eligible) return { kind: "ok", label: "ready" };
  const chip = wordsFor(PHASE_CHIPS, row.phase) ?? { kind: "", label: "unknown" };
  const pct = Number.isFinite(row.pct) ? Math.min(100, Math.max(0, Math.round(row.pct))) : 0;
  console.assert(pct >= 0 && pct <= 100, "engineChip: pct clamped");
  return row.phase === "downloading" ? { kind: chip.kind, label: `${chip.label} ${pct}%` } : chip;
}

/** The engine row's second chip, only while its breaker is open (`health: "unhealthy"`): the
 *  page browser is paused after repeated failures and tries again by itself. null otherwise. */
export function healthChip(row: BrowserEngineStatus): (ChipWords & { title: string }) | null {
  console.assert(row.health === undefined || typeof row.health === "string", "healthChip: health shape");
  const paused = row.health === "unhealthy";
  console.assert(typeof paused === "boolean", "healthChip: a verdict");
  return paused
    ? { kind: "warn", label: "paused after repeated failures",
        title: "The page browser is paused after repeated failures and tries again by itself in a few minutes." }
    : null;
}

/** Why the engine can or cannot read pages here, in one sentence. */
export function engineReason(row: BrowserEngineStatus): string {
  console.assert(typeof row.reason === "string", "engineReason: reason is a string");
  console.assert(typeof row.phase === "string", "engineReason: phase is a string");
  if (row.eligible) return READY_SENTENCE;
  if (row.reason) return wordsFor(REASON_SENTENCES, row.reason) ?? UNKNOWN_SENTENCE;
  // the installer's own fixed sentence (it names github.com, never a path or an exception)
  if (row.phase === "error" && typeof row.error === "string" && row.error.trim()) return row.error;
  const byPhase = wordsFor(PHASE_REASON, row.phase);
  return (byPhase && wordsFor(REASON_SENTENCES, byPhase)) || UNKNOWN_SENTENCE;
}

/** "v0.2.4, released 12 days ago" — the pinned build and how old it is. */
export function engineMeta(row: BrowserEngineStatus): string {
  console.assert(typeof row.version === "string", "engineMeta: version is a string");
  const days = Number.isInteger(row.age_days) && row.age_days >= 0 ? row.age_days : null;
  console.assert(days === null || days >= 0, "engineMeta: age never negative");
  const age = days === null ? "" : days === 0 ? ", released today"
    : days === 1 ? ", released yesterday" : `, released ${days} days ago`;
  return `v${row.version}${age}`;
}

/** The sandbox the engine runs inside here, and when it last passed its check in this app run. */
export function sandboxLine(row: BrowserEngineStatus): string {
  console.assert(row.sandbox === undefined || typeof row.sandbox === "object", "sandboxLine: sandbox shape");
  const words = wordsFor(SANDBOX_WORDS, row.sandbox?.kind);
  if (!words) return "Sandbox: none available here";
  const at = parseTs(row.sandbox.verified_at ?? null);
  console.assert(at === null || !Number.isNaN(at.getTime()), "sandboxLine: a real instant");
  if (!at) return `Sandbox: ${words} — not checked since SmartBrain started`;
  const when = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(at);
  return `Sandbox: ${words} — passed its check ${when}`;
}

/** The card flow's worker chip: a render running now, or idle. */
export function workerChip(flow: BrowsersFlow): ChipWords {
  console.assert(typeof flow === "object" && flow !== null, "workerChip: the flow counts");
  const busy = flow.worker === "busy";
  console.assert(typeof busy === "boolean", "workerChip: a verdict");
  return busy ? { kind: "accent", label: "reading a page" } : { kind: "", label: "idle" };
}
