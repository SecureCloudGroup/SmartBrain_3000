<script lang="ts">
  // One stroke icon family (vendored Lucide subset — see lib/icons.ts), currentColor so it
  // themes for free. Decorative by default (aria-hidden); pass `label` when the icon IS the
  // content (e.g. an icon-only button without its own aria-label).
  import { ICONS, type IconName } from "$lib/icons";

  let { name, size = 16, label = "" }: { name: IconName; size?: number; label?: string } = $props();
</script>

<svg
  viewBox="0 0 24 24"
  width={size}
  height={size}
  fill="none"
  stroke="currentColor"
  stroke-width="2"
  stroke-linecap="round"
  stroke-linejoin="round"
  role={label ? "img" : undefined}
  aria-label={label || undefined}
  aria-hidden={label ? undefined : true}
  style="vertical-align: -0.18em"
>
  <!-- eslint-disable-next-line svelte/no-at-html-tags -- generated static path data, not user input -->
  {@html ICONS[name]}
</svg>
