// Pure "which chips describe this vault's state?" mapper. The knowledge page renders whatever
// this returns; extracting it here keeps the state matrix testable (each state → its chip set)
// and keeps the Svelte template free of nested conditionals it would otherwise grow every time
// the lifecycle gained another signal.
//
// One vault always maps to ONE chip list, in the order listed — Public/Retired/Subscribed/etc.
// is the "hero" chip (always first), fingerprint follows (never a badge without an identity),
// then version / date / secondary status. A helper `vaultChipsSummary` returns a short muted
// line for e.g. "published 2026-08-01" — text that lives outside the chip row.
//
// The functions here are pure: no fetch, no DOM, no I/O. All input comes from a Vault; all
// output is plain data the Svelte template renders.
import type { IconName } from "$lib/icons";
import type { Vault } from "$lib/api";

// Chip kinds mirror Chip.svelte's contract. "" is the neutral/muted pill (grey border, muted
// text on panel) — the "positive indicator" the task calls for on a Private vault.
export type ChipKind = "" | "accent" | "ok" | "warn" | "danger";

export interface VaultChip {
  key: string;         // stable key for {#each}
  kind: ChipKind;
  label: string;
  icon?: IconName;
  mono?: boolean;      // fingerprints only — read/compared character by character
  title?: string;      // hover tooltip (screenreaders read it too when the label is short)
}

// -- small guards ------------------------------------------------------------------------------

function isSubscription(v: Vault): boolean {
  return v.kind === "imported" && Boolean(v.source?.url);
}

function isImportedFile(v: Vault): boolean {
  return v.kind === "imported" && !v.source?.url;
}

// The public version to display, with a legacy fallback: vaults published before
// published_seq existed have it null while published_open is true — use the internal seq (the
// value the export handler bumped just before pack()) rather than showing nothing.
function publicVersion(v: Vault): number {
  console.assert(v.published_open, "publicVersion: only meaningful once published_open");
  console.assert(typeof v.internal_seq === "number", "publicVersion: internal_seq required");
  return v.published_seq ?? v.internal_seq;
}

// -- publisher-side chips (kind === "local") ---------------------------------------------------

function publisherChips(v: Vault): VaultChip[] {
  console.assert(v.kind === "local", "publisherChips: local vaults only");
  console.assert(Array.isArray(v.tags), "publisherChips: tags must be an array");
  // Retired-publisher: replaces the Public accent with a muted "Retired v{n}" — the version is
  // what subscribers pinned against, and the muted kind reads as "this is done" rather than an
  // active status. Fingerprint still travels (identity never leaves).
  if (v.published_open && v.retired_published) {
    return [
      { key: "retired-pub", kind: "", label: `Retired v${publicVersion(v)}`,
        title: "You retired this vault — subscribers keep their documents but stop checking" },
      ...fingerprintChipsPublisher(v),
    ];
  }
  if (v.published_open) {
    const chips: VaultChip[] = [
      { key: "public", kind: "accent", label: `Public v${publicVersion(v)}`,
        title: "The published version — subscribers pin this seq and pick up newer ones" },
      ...fingerprintChipsPublisher(v),
    ];
    if (v.internal_seq > publicVersion(v)) {
      // Sealed shares bumped internal_seq past the last open publish — the user has content
      // that hasn't gone out on the public channel yet. Warn-lite ("warn" kind is subtle
      // yellow, not the red "danger"), so it reads as a nudge, not a problem.
      chips.push({ key: "unpub", kind: "warn", label: "Unpublished changes",
        title: `Internal v${v.internal_seq} is ahead of public v${publicVersion(v)} — re-export to publish` });
    }
    return chips;
  }
  if (v.shared_sealed) {
    // Sealed-shared, never open-published: a neutral pill so it doesn't compete with Public/Ok.
    return [{ key: "sealed", kind: "", label: "Shared · sealed",
      title: "You've shared this as a sealed file — the receiver needs the vault key" }];
  }
  // Never shared, purely local: a POSITIVE indicator, not the absence of one. Muted so it
  // doesn't shout — a private vault is the default, and a badge shouting "PRIVATE" would.
  return [{ key: "private", kind: "", label: "Private",
    title: "This vault stays on your device — you haven't shared it" }];
}

function fingerprintChipsPublisher(v: Vault): VaultChip[] {
  if (!v.publisher_fingerprint) return [];
  return [{ key: "pubfp", kind: "", label: v.publisher_fingerprint, mono: true,
    title: "Your publisher fingerprint — how subscribers identify you" }];
}

// -- subscriber / imported chips ---------------------------------------------------------------

function subscribedFingerprintChip(v: Vault): VaultChip | null {
  if (!v.pinned_fingerprint) return null;
  return { key: "pinfp", kind: "", label: v.pinned_fingerprint, mono: true,
    title: "The pinned publisher — every update must be signed by this identity" };
}

function subscriptionChips(v: Vault): VaultChip[] {
  console.assert(isSubscription(v), "subscriptionChips: subscription only");
  // Blocked, retired, unreachable each REPLACE the green Subscribed chip — they are the
  // dominant state. Only one can be true (retired/unreachable exclude each other in
  // _due_subscriptions, and blocked overrides all in the route handlers).
  if (v.source?.blocked) {
    return [
      { key: "blocked", kind: "danger", label: "Blocked",
        title: "The publisher's key changed — updates are refused until you confirm" },
      ...maybeFp(subscribedFingerprintChip(v)),
    ];
  }
  if (v.source?.retired) {
    return [
      { key: "sub-retired", kind: "", label: "Retired by publisher",
        title: "The publisher retired this vault — your documents stay, checking stops" },
      ...maybeFp(subscribedFingerprintChip(v)),
    ];
  }
  if (v.source?.unreachable) {
    return [
      { key: "unreachable", kind: "warn", label: "Unreachable",
        title: unreachableTitle(v.source.unreachable_reason) },
      ...maybeFp(subscribedFingerprintChip(v)),
    ];
  }
  const chips: VaultChip[] = [
    { key: "subscribed", kind: "ok", icon: "check", label: "Subscribed" },
    ...maybeFp(subscribedFingerprintChip(v)),
  ];
  if (typeof v.source?.seq === "number") {
    chips.push({ key: "seq", kind: "", label: `v${v.source.seq}`,
      title: "The version you currently have (the seq you're pinned at)", mono: true });
  }
  return chips;
}

function importedFileChips(v: Vault): VaultChip[] {
  console.assert(isImportedFile(v), "importedFileChips: file-imported vaults only");
  // Imported (not a URL subscription): a neutral chip, plus the pinned fingerprint (attached
  // by the same _attach_pinned_fp path as subscribed vaults — the identity is available
  // either way and a subscriber needs it to match a friend's fingerprint).
  return [
    { key: "imported", kind: "", label: "Imported" },
    ...maybeFp(subscribedFingerprintChip(v)),
  ];
}

function maybeFp(chip: VaultChip | null): VaultChip[] {
  return chip ? [chip] : [];
}

function unreachableTitle(reason: "took_down" | "dead_host" | undefined): string {
  if (reason === "took_down") return "The publisher took this vault down";
  if (reason === "dead_host") return "The host hasn't answered for a week";
  return "The publisher's host stopped responding";
}

// -- entry point -------------------------------------------------------------------------------

/**
 * The chips that describe this vault's state, in render order. One vault → one list; the
 * Svelte template does no further branching on state (only on tag chips, which live in a
 * separate row and don't collide with the state chips).
 */
export function vaultChips(v: Vault): VaultChip[] {
  console.assert(v !== null && typeof v === "object", "vaultChips: vault required");
  console.assert(typeof v.kind === "string", "vaultChips: vault.kind required");
  if (v.kind === "local") return publisherChips(v);
  if (isSubscription(v)) return subscriptionChips(v);
  return importedFileChips(v);
}

/**
 * A short muted line for secondary metadata that shouldn't crowd the chip row. Today: the
 * publisher's declared publish date on a subscription (both live and retired), so a subscriber
 * knows WHEN what they're reading was actually written; empty when nothing to show.
 */
export function vaultChipsSummary(v: Vault): string {
  console.assert(v !== null && typeof v === "object", "vaultChipsSummary: vault required");
  console.assert(typeof v.kind === "string", "vaultChipsSummary: vault.kind required");
  if (!isSubscription(v)) return "";
  const date = v.published_at;
  if (!date) return "";
  return `published ${date}`;
}

/**
 * The short human copy to render on a subscription's card when the publisher retired the
 * vault — a fact the chip alone shouldn't have to carry. Empty when the subscription isn't
 * in the retired state.
 */
export function retiredSubscriptionNote(v: Vault): string {
  console.assert(v !== null && typeof v === "object", "retiredSubscriptionNote: vault required");
  console.assert(typeof v.kind === "string", "retiredSubscriptionNote: vault.kind required");
  if (!isSubscription(v) || !v.source?.retired) return "";
  return "The publisher retired this vault — your documents stay in Knowledge and remain readable. Auto-update stopped.";
}

/**
 * The short human copy for an unreachable subscription — reason-specific, so the user sees
 * the DIFFERENCE between "the publisher took it down" and "the host has been dead for a week".
 * Empty when the subscription isn't unreachable.
 */
export function unreachableSubscriptionNote(v: Vault): string {
  console.assert(v !== null && typeof v === "object", "unreachableSubscriptionNote: vault required");
  console.assert(typeof v.kind === "string", "unreachableSubscriptionNote: vault.kind required");
  if (!isSubscription(v) || !v.source?.unreachable) return "";
  if (v.source.unreachable_reason === "took_down") {
    return "The publisher took this vault down. Auto-update stopped; a manual check will still try again.";
  }
  return "The host hasn't answered for a week. Auto-update stopped; a manual check will still try again.";
}
