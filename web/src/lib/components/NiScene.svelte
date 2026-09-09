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
  import { sparkPath, sparkBars, gaugeArc } from "$lib/ni/sparkline";

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

  // Spark/gauge use the same tone→token map as everything else. A `default` or
  // absent tone maps to accent (mirrors the bar's "default paints as accent"
  // rule) so a chartable node always has a visible stroke/fill.
  const SPARK_W = 200;
  const SPARK_H = 30;
  const GAUGE_R = 40;
  const GAUGE_TRACK_D = gaugeArc(1, 0, 1, GAUGE_R).d;

  function sparkColor(tone: Tone | undefined): string {
    console.assert(tone === undefined || tone in TONE_VAR, "sparkColor: tone in enum");
    console.assert(typeof TONE_VAR === "object", "sparkColor: TONE_VAR present");
    const t: Tone = tone && tone !== "default" ? tone : "accent";
    return TONE_VAR[t];
  }

  function clampValue(v: number, lo: number, hi: number): number {
    console.assert(typeof v === "number", "clampValue: v is number");
    console.assert(hi > lo, "clampValue: hi > lo");
    return Math.min(hi, Math.max(lo, v));
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
{:else if node.type === "spark"}
  {#if node.points.length === 0}
    <span class="muted ni-spark-empty" role="img" aria-label="trend">—</span>
  {:else}
    <svg
      class="ni-spark"
      viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="trend"
      style={`color: ${sparkColor(node.tone)}`}
    >
      {#if node.kind === "line"}
        <polyline
          points={sparkPath(node.points, SPARK_W, SPARK_H)}
          fill="none"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-linejoin="round"
          stroke-linecap="round"
          vector-effect="non-scaling-stroke"
        />
      {:else}
        {#each sparkBars(node.points, SPARK_W, SPARK_H) as r, i (i)}
          <rect x={r.x} y={r.y} width={r.w} height={r.h} fill="currentColor" />
        {/each}
      {/if}
    </svg>
  {/if}
{:else if node.type === "gauge"}
  <div class="ni-gauge">
    <svg
      class="ni-gauge-svg"
      viewBox="-48 -48 96 72"
      role="meter"
      aria-label={node.label ?? "gauge"}
      aria-valuenow={clampValue(node.value, node.min, node.max)}
      aria-valuemin={node.min}
      aria-valuemax={node.max}
      style={`color: ${sparkColor(node.tone)}`}
    >
      <path
        d={GAUGE_TRACK_D}
        fill="none"
        stroke="var(--accent-tint)"
        stroke-width="6"
        stroke-linecap="round"
      />
      {#if gaugeArc(node.value, node.min, node.max, GAUGE_R).pct > 0}
        <path
          d={gaugeArc(node.value, node.min, node.max, GAUGE_R).d}
          fill="none"
          stroke="currentColor"
          stroke-width="6"
          stroke-linecap="round"
        />
      {/if}
    </svg>
    <div class="ni-gauge-value">{formatNumber(node.value, "plain")}</div>
    {#if node.label}
      <div class="ni-gauge-label">{node.label}</div>
    {/if}
  </div>
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
  /* Spark + gauge sit inline like any other content node — no motion (charter),
     tokens only for color. Non-uniform SVG scaling is fine for fills; the line's
     `vector-effect: non-scaling-stroke` keeps stroke width visually consistent. */
  .ni-spark {
    display: block;
    width: 100%;
    height: 30px;
  }
  .ni-spark-empty {
    font-size: var(--f-label);
    color: var(--muted);
  }
  .ni-gauge {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--s-1);
  }
  .ni-gauge-svg {
    /* viewBox is 96x72 — keep the 4:3 ratio so the arc's rounded caps
       (y up to +20 + stroke) are never clipped. */
    width: 120px;
    height: 90px;
  }
  .ni-gauge-value {
    font-size: var(--f-h2);
    font-variant-numeric: tabular-nums;
    color: var(--text);
    line-height: var(--lh-ui);
  }
  .ni-gauge-label {
    font-size: var(--f-label);
    color: var(--muted);
    line-height: var(--lh-ui);
  }
</style>
