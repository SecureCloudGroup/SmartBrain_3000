<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import Spinner from "$lib/components/Spinner.svelte";

  onMount(() => goto("/settings/providers"));
</script>

<Spinner block />
