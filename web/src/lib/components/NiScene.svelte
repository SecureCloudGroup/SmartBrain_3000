<script lang="ts">
  // Audited recursive renderer for a BOUND Neural Interface scene (ni-format §5).
  // The server-side binder has already inlined every {"$bind": …}; this file only
  // switches on `node.type`, uses design tokens for every color, and never emits
  // {@html}. validateBoundScene at the top guards against a malformed subtree —
  // depth is capped at 8 by the spec, so recursion is bounded (self-import: the
  // Svelte 5 replacement for svelte:self).
  import Chip from "./Chip.svelte";
  import Icon from "./Icon.svelte";
  import Self from "./NiScene.svelte";
  import { ICONS, type IconName } from "$lib/icons";
  import {
    formatNumber,
    validateBoundScene,
    type SceneNode,
    type Tone,
  } from "$lib/ni/scene";

  let { node }: { node: SceneNode } = $props();

  // Validate the whole subtree — bounded by the spec's 100-node/depth-8 caps, cheap
  // in practice. Any invalid node refuses the WHOLE card to a quiet "unrenderable"
  // placeholder (never a thrown error) — we don't try to render around a bad node.
  // (validateBoundScene itself refuses a null/non-object root, so top-level pre-checks
  // are redundant — Svelte 5 would flag reading a $props() rune at script-top anyway.)
  const err = $derived(validateBoundScene(node));

  const TONE_VAR: Record<Tone, string> = {
    default: "var(--text)",
    muted: "var(--muted)",
    accent: "var(--accent)",
    ok: "var(--ok)",
    warn: "var(--warn)",
    danger: "var(--danger)",
  };
  // Text roles pick their type-scale + baseline color; a non-default tone overrides
  // the color. `value` gets tabular numerals so aligned rows don't jitter.
  const ROLE_STYLE = {
    title: "font-size: var(--f-section); font-weight: 650",
    label: "font-size: var(--f-label); color: var(--muted)",
    value: "font-size: var(--f-h2); font-variant-numeric: tabular-nums",
    caption: "font-size: var(--f-meta); color: var(--muted)",
  } as const;
  const NUM_SIZE = { sm: "var(--f-label)", md: "var(--f-h2)", lg: "var(--f-h1)" } as const;
  const GAP = { sm: "var(--s-2)", md: "var(--s-4)" } as const;

  function toneStyle(tone: Tone): string {
    console.assert(typeof tone === "string", "toneStyle: tone is string");
    console.assert(tone in TONE_VAR, "toneStyle: tone in enum");
    return tone === "default" ? "" : `color: ${TONE_VAR[tone]}`;
  }

  function textStyle(role: keyof typeof ROLE_STYLE, tone: Tone): string {
    console.assert(role in ROLE_STYLE, "textStyle: role in enum");
    console.assert(tone in TONE_VAR, "textStyle: tone in enum");
    const base = ROLE_STYLE[role];
    return tone === "default" ? base : `${base}; color: ${TONE_VAR[tone]}`;
  }

  function barPct(value: number, max: number): number {
    console.assert(typeof value === "number", "barPct: value is number");
    console.assert(typeof max === "number" && max > 0, "barPct: max > 0");
    return Math.min(100, Math.max(0, (value / max) * 100));
  }
</script>

{#if err}
  <span class="muted ni-bad" title={err}>unrenderable</span>
{:else if node.type === "stack"}
  <div
    class="ni-stack"
    style:flex-direction={node.dir === "v" ? "column" : "row"}
    style:gap={GAP[node.gap]}
  >
    {#each node.children as child, i (i)}
      <Self node={child} />
    {/each}
  </div>
{:else if node.type === "grid"}
  <div
    class="ni-grid"
    style:grid-template-columns={`repeat(${node.cols}, minmax(0, 1fr))`}
  >
    {#each node.children as child, i (i)}
      <Self node={child} />
    {/each}
  </div>
{:else if node.type === "divider"}
  <hr class="ni-divider" />
{:else if node.type === "text"}
  <span class="ni-text" style={textStyle(node.role, node.tone)}>{node.value}</span>
{:else if node.type === "number"}
  <span
    class="ni-number"
    style={`font-size: ${NUM_SIZE[node.size]}; font-variant-numeric: tabular-nums; ${toneStyle(node.tone)}`}
  >{formatNumber(node.value, node.format, node.unit)}</span>
{:else if node.type === "chip"}
  <Chip kind={node.kind}>{node.value}</Chip>
{:else if node.type === "bar"}
  <div
    class="ni-bar"
    role="progressbar"
    aria-label="progress"
    aria-valuenow={Math.min(node.max, Math.max(0, node.value))}
    aria-valuemin={0}
    aria-valuemax={node.max}
  >
    <div
      class="ni-bar-fill"
      style={`width: ${barPct(node.value, node.max)}%; background: ${TONE_VAR[node.tone === "default" ? "accent" : node.tone]}`}
    ></div>
  </div>
{:else if node.type === "icon"}
  {#if Object.hasOwn(ICONS, node.name)}
    <span style={toneStyle(node.tone)}>
      <Icon name={node.name as IconName} />
    </span>
  {:else}
    <span class="muted" title={`unknown icon: ${node.name}`}>·</span>
  {/if}
{/if}

<style>
  .ni-stack { display: flex; }
  .ni-grid { display: grid; gap: var(--s-3); }
  .ni-divider {
    border: 0;
    border-top: 1px solid var(--border);
    margin: var(--s-2) 0;
    width: 100%;
  }
  .ni-text, .ni-number { color: var(--text); line-height: var(--lh-ui); }
  .ni-bad { font-size: var(--f-meta); font-style: italic; }
  .ni-bar {
    width: 100%;
    height: 8px;
    border-radius: var(--r-full);
    background: var(--accent-tint);
    overflow: hidden;
  }
  .ni-bar-fill {
    height: 100%;
    border-radius: var(--r-full);
    /* Motion law (app.css §motion): transitions are transform/opacity only. A width
       tween would violate that and re-layout every frame — the bar just snaps. */
  }
</style>
