<script lang="ts">
  // Small header chip shown only when the app is in remote mode (reached over WebRTC).
  // On the Desktop LAN the status stays "idle" and nothing renders.
  import { remote } from "$lib/remote/connection.svelte";

  const LABEL: Record<string, string> = {
    connecting: "Remote · connecting…",
    verifying: "Remote · verifying Desktop…",
    connected: "Remote · connected",
    "connected-direct": "Remote · direct (P2P)",
    "connected-relay": "Remote · relayed",
    reconnecting: "Remote · reconnecting…",
    untrusted: "Remote · BLOCKED",
    offline: "Remote · offline",
  };
  const tone = $derived(
    remote.status === "connected-direct"
      ? "ok"
      : remote.status === "connected-relay"
        ? "warn"
        : remote.status === "untrusted"
          ? "bad"
          : "muted",
  );
  const tip = $derived(
    remote.status === "connected-relay"
      ? "Direct wasn't possible, so traffic goes through an encrypted relay that can't read it."
      : remote.status === "untrusted"
        ? "Couldn't verify your Desktop's identity — connection blocked. Re-pair if you reinstalled."
        : remote.detail,
  );
</script>

{#if remote.status !== "idle" && remote.status !== "untrusted"}
  <!-- BLOCKED (untrusted) renders as a full-width banner from the layout instead of this chip;
       a possible-MITM warning must not be easy to miss in a tiny appbar pill. -->
  <span class="remote-chip {tone}" title={tip}>{LABEL[remote.status] ?? remote.status}</span>
{/if}

<style>
  .remote-chip {
    font-size: 0.75rem;
    padding: 0.15rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--border, #4444);
    white-space: nowrap;
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
