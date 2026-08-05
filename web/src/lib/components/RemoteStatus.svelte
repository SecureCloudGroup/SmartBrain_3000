<script lang="ts">
  // Small header chip shown only when the app is in remote mode (reached over WebRTC).
  // On the Desktop LAN the status stays "idle" and nothing renders.
  //
  // "Locked" is derived (see chip.ts): a locked Desktop still answers over the encrypted
  // bridge, so the phone must distinguish "Desktop is locked" (bridge up, vault locked)
  // from "Desktop is unreachable" (can't get on the bridge at all — off, asleep, no path).
  import { remote } from "$lib/remote/connection.svelte";
  import { account } from "$lib/account.svelte";
  import { chipState } from "$lib/remote/chip";
  import Icon from "$lib/components/Icon.svelte";

  const state = $derived(chipState(remote.status, account.status?.unlocked ?? null));

  const LABEL: Record<string, string> = {
    connecting: "Remote · connecting…",
    verifying: "Remote · verifying Desktop…",
    connected: "Remote · connected",
    "connected-direct": "Remote · direct (P2P)",
    "connected-relay": "Remote · relayed",
    reconnecting: "Remote · reconnecting…",
    untrusted: "Remote · BLOCKED",
    offline: "Remote · unreachable",
    locked: "Desktop locked",
  };
  const tone = $derived(
    state === "connected-direct"
      ? "ok"
      : state === "connected-relay"
        ? "warn"
        : state === "locked"
          ? "warn"
          : state === "untrusted"
            ? "bad"
            : "muted",
  );
  const tip = $derived(
    state === "connected-relay"
      ? "Direct wasn't possible, so traffic goes through an encrypted relay that can't read it."
      : state === "untrusted"
        ? "Couldn't verify your Desktop's identity — connection blocked. Re-pair if you reinstalled."
        : state === "locked"
          ? "Your Desktop is locked. Tap to unlock — the bridge is up, but nothing here works until it's unlocked."
          : remote.detail,
  );
</script>

{#if state !== "idle" && state !== "untrusted"}
  <!-- BLOCKED (untrusted) renders as a full-width banner from the layout instead of this chip;
       a possible-MITM warning must not be easy to miss in a tiny appbar pill.
       Locked is tap-through to /unlock — the unlock endpoint works over the bridge today
       (no desktop-local fence on POST /api/account/unlock), so the user can act right here. -->
  {#if state === "locked"}
    <a class="remote-chip {tone}" href="/unlock" title={tip}>
      <Icon name="lock" /> {LABEL[state]}
    </a>
  {:else}
    <span class="remote-chip {tone}" title={tip}>{LABEL[state] ?? state}</span>
  {/if}
{/if}

<style>
  .remote-chip {
    font-size: 0.75rem;
    padding: 0.15rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--border, #4444);
    white-space: nowrap;
    text-decoration: none; /* the locked variant is an <a>; keep it looking like a chip, not a link */
  }
  /* Theme tokens (not hardcoded hex): the light-theme --ok/--warn meet 4.5:1 contrast. */
  .remote-chip.ok {
    color: var(--ok);
    border-color: color-mix(in srgb, var(--ok) 40%, transparent);
  }
  .remote-chip.warn {
    color: var(--warn);
    border-color: color-mix(in srgb, var(--warn) 40%, transparent);
  }
  .remote-chip.bad {
    color: var(--danger);
    border-color: color-mix(in srgb, var(--danger) 40%, transparent);
    font-weight: 600;
  }
  .remote-chip.muted {
    color: var(--muted);
  }
</style>
