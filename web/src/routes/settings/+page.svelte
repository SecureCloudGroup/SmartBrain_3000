<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";

  onMount(() => goto("/settings/providers"));
</script>

<p class="muted">Loading&hellip;</p>
