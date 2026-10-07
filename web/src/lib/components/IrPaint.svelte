<script lang="ts">
  // IrPaint paints a frozen CLIR (app/smartbrain_3000/ni_forms/types.py) that the server
  // laid out and linted. The client never lints, lays out or hashes: it validates the
  // shape, resolves the live bindings at `now`, and paints positioned DOM text over
  // inline SVG. The live-binding math is $lib/ni/clir.ts, a port of paint/live.py.
  import { onDestroy } from "svelte";
  import { loadWorld, mapPaths, type Rings } from "$lib/ni/basemap";
  import {
    renderModel,
    validateClir,
    ClirError,
    resolveX,
    type Clir,
    type RenderModel,
    type RenderOp,
    type RenderText,
    type Prim,
    type XAnchor,
    type RectPrim,
    type LinePrim,
    type TriPrim,
    type DotPrim,
    type PathPrim,
    type CellsPrim,
    type ImagePrim,
    type BasemapPrim,
    type IconPrim,
  } from "$lib/ni/clir";
  import { NI_ICONS } from "$lib/ni/icons.generated";
  import { detectOverflows } from "$lib/ni/overflow";

  let { clir, viewerTz }: { clir: unknown; viewerTz: string } = $props();

  // Live bindings tick every 15 s (variant switches, past-dim boundaries, countdowns).
  // `window.NI_NOW` freezes the clock for screenshots, as the Python painter's --now does.
  function nowSource(): number {
    const w = typeof window === "undefined" ? null : (window as unknown as { NI_NOW?: unknown });
    return typeof w?.NI_NOW === "number" ? w.NI_NOW : Date.now();
  }
  let nowMs = $state(nowSource());
  const tick = typeof window === "undefined"
    ? null
    : window.setInterval(() => { nowMs = nowSource(); }, 15000);
  onDestroy(() => { if (tick !== null) window.clearInterval(tick); });

  // Validate once per CLIR. A ClirError never escapes: the component shows the same
  // muted "unrenderable" placeholder NiScene uses for a scene it cannot draw.
  const parsed = $derived.by((): { ok: true; clir: Clir } | { ok: false; why: string } => {
    try {
      return { ok: true, clir: validateClir(clir) };
    } catch (e) {
      return { ok: false, why: e instanceof ClirError ? e.message : String(e) };
    }
  });

  // The plane is as wide as the card's content box (measured on the wrapper). The CLIR
  // was linted for the bucket [min_w, max_w] and its anchors (l/r/c/f) resolve against
  // the real width, so a cell wider than max_w simply gives the design more room. A cell
  // narrower than the floor would make prims overlap, so the plane is painted at the
  // floor and scaled down to fit.
  let cellW = $state(0);
  const floorW = $derived(parsed.ok ? parsed.clir.bucket.min_w : 0);
  const paintW = $derived(cellW > floorW ? cellW : floorW);
  const scale = $derived(cellW > 0 && cellW < floorW ? cellW / floorW : 1);
  const model = $derived<RenderModel | null>(
    parsed.ok && paintW > 0
      ? renderModel(parsed.clir, { width: paintW, now: nowMs, viewerTz })
      : null,
  );

  // Overflow sentinel: once the fonts are ready, any text wider than its laid-out max_w
  // is marked and counted. Nothing is re-laid out; the CSS ellipsis is the safety net.
  let plane = $state<HTMLDivElement | null>(null);
  $effect(() => {
    if (!plane || !model) return;
    const root = plane;
    const check = () => { detectOverflows(root); };
    const fonts = typeof document === "undefined" ? null : document.fonts;
    if (fonts && typeof fonts.ready?.then === "function") fonts.ready.then(check, check);
    else check();
  });

  // Coastlines: the land rings vector.py draws (world110m.json, served at /ni/) are fetched
  // once per page when a card first shows a basemap prim. Until they arrive the land box
  // paints as before, so nothing shifts; a failed load keeps the box.
  const uid = $props.id();
  let world = $state.raw<Rings | null>(null);
  const hasBasemap = $derived(model !== null && model.ops.some((op) => svgPrim(op)?.k === "basemap"));
  $effect(() => {
    if (!hasBasemap) return;
    let live = true;
    loadWorld().then(
      (rings) => { if (live) world = rings; },
      () => { if (live) world = []; }, // loadWorld resolves [] on failure; this is the belt
    );
    return () => { live = false; };
  });

  function fmt(v: number): string {
    console.assert(Number.isFinite(v), "fmt: finite number");
    const s = v.toFixed(2).replace(/\.?0+$/, "");
    return s === "-0" || s === "" ? "0" : s;
  }
  function rx(x: XAnchor, w: number): number {
    console.assert(w >= 0, "rx: width non-negative");
    return resolveX(x, 0, w);
  }
  /** Token name → CSS custom property. The names come from tokens.json; theme-vars.test
   *  checks that every generated `--ni-*` name is defined in app.css for both themes. */
  function tokVar(tok: string): string {
    console.assert(tok.length > 0, "tokVar: token required");
    return `var(--ni-${tok})`;
  }
  function isText(op: RenderOp): op is RenderText {
    return op.kind === "text";
  }
  function svgPrim(op: RenderOp): Prim | null {
    return op.kind === "svg" ? op.prim : null;
  }

  function boxRect(b: { x0: XAnchor; x1: XAnchor; y0: number; y1: number }, w: number) {
    console.assert(b.y1 >= b.y0, "boxRect: ordered box");
    console.assert(w >= 0, "boxRect: width non-negative");
    const x0 = rx(b.x0, w), x1 = rx(b.x1, w);
    return { x: x0, y: b.y0, w: Math.max(0, x1 - x0), h: Math.max(0, b.y1 - b.y0) };
  }

  function triPoints(p: TriPrim, w: number): string {
    console.assert(p.k === "tri", "triPoints: tri prim");
    console.assert(p.size > 0, "triPoints: size positive");
    const h = p.size * 0.866, x = rx(p.x, w), y = p.y, s = p.size;
    const pts: Array<[number, number]> = p.dir === "up"
      ? [[x - s / 2, y + h / 2], [x + s / 2, y + h / 2], [x, y - h / 2]]
      : p.dir === "down"
        ? [[x - s / 2, y - h / 2], [x + s / 2, y - h / 2], [x, y + h / 2]]
        : p.dir === "left"
          ? [[x + h / 2, y - s / 2], [x + h / 2, y + s / 2], [x - h / 2, y]]
          : [[x - h / 2, y - s / 2], [x - h / 2, y + s / 2], [x + h / 2, y]];
    return pts.map(([a, b]) => `${fmt(a)},${fmt(b)}`).join(" ");
  }

  function pathD(p: PathPrim, w: number): { line: string; fill: string | null } {
    console.assert(p.k === "path", "pathD: path prim");
    console.assert(Array.isArray(p.pts), "pathD: points array");
    const b = p.box, x0 = rx(b.x0, w), x1 = rx(b.x1, w);
    const abs = p.pts.map(([fx, fy]) =>
      [x0 + fx * (x1 - x0), b.y0 + fy * (b.y1 - b.y0)] as [number, number]);
    if (abs.length < 2) return { line: "", fill: null };
    const line = "M" + abs.map(([a, c]) => `${fmt(a)},${fmt(c)}`).join(" L");
    const first = abs[0], last = abs[abs.length - 1];
    const fill = p.fill
      ? `${line} L${fmt(last[0])},${fmt(b.y1)} L${fmt(first[0])},${fmt(b.y1)} Z`
      : null;
    return { line, fill };
  }

  /** A now line that must not cross its labels is one <line> whose dash pattern skips
   *  the label gaps: the same segments paint/live.py computes for the Python painter. */
  function gapDash(segs: Array<[number, number]>, ya: number, yb: number): string {
    console.assert(segs.length > 0, "gapDash: segments required");
    console.assert(ya <= yb, "gapDash: ordered ends");
    const arr: number[] = [0];
    let pos = ya;
    for (const [s0, s1] of segs) { arr.push(s0 - pos, s1 - s0); pos = s1; }
    arr.push(yb - pos + 1000);
    return arr.map(fmt).join(" ");
  }

  function cellFill(v: number, ramp: "seq" | "div", steps: number): string {
    console.assert(steps > 0, "cellFill: steps positive");
    console.assert(v >= 0, "cellFill: value non-negative");
    if (ramp === "seq") {
      return tokVar(`viz-seq-${1 + Math.round(v * 4 / Math.max(1, steps - 1))}`);
    }
    const t = v / Math.max(1, steps - 1);
    return t <= 0.5
      ? `color-mix(in srgb, ${tokVar("viz-div-mid")} ${fmt(t * 200)}%, ${tokVar("viz-div-neg")})`
      : `color-mix(in srgb, ${tokVar("viz-div-pos")} ${fmt((t - 0.5) * 200)}%, ${tokVar("viz-div-mid")})`;
  }

  interface Cell { x: number; y: number; w: number; h: number; style: string }
  function cellsFor(p: CellsPrim, w: number): Cell[] {
    console.assert(p.k === "cells", "cellsFor: cells prim");
    console.assert(p.cols > 0 && p.rows > 0, "cellsFor: grid shape");
    const b = p.box, x0 = rx(b.x0, w), x1 = rx(b.x1, w);
    const cw = (x1 - x0 - p.gap * (p.cols - 1)) / p.cols;
    const ch = (b.y1 - b.y0 - p.gap * (p.rows - 1)) / p.rows;
    const out: Cell[] = [];
    for (let i = 0; i < p.v.length; i++) {
      const r = Math.floor(i / p.cols), c = i % p.cols, v = p.v[i];
      // A cell's fill is data (its value picks a ramp step), so it is inline by necessity.
      const style = v < 0
        ? `fill:none;stroke:${tokVar("viz-grid")};stroke-width:1`
        : `fill:${cellFill(v, p.ramp, p.steps)}`;
      out.push({ x: x0 + c * (cw + p.gap), y: b.y0 + r * (ch + p.gap), w: cw, h: ch, style });
    }
    return out;
  }

  function iconScale(ic: IconPrim): number {
    console.assert(ic.size > 0, "iconScale: size positive");
    console.assert(NI_ICONS.box > 0, "iconScale: icon box");
    return ic.size / NI_ICONS.box;
  }
  function pointList(pts: Array<[number, number]>): string {
    console.assert(Array.isArray(pts), "pointList: points array");
    return pts.map((pt) => `${fmt(pt[0])},${fmt(pt[1])}`).join(" ");
  }
</script>

{#if !parsed.ok || !model}
  <span class="muted ni-bad" title={parsed.ok ? "render failed" : parsed.why}>unrenderable</span>
{:else}
  <!-- The wrapper measures the card's content width; the plane is the prim coordinate
       system. Width, height and scale are per-CLIR geometry, hence inline. -->
  <div class="ir-wrap" bind:clientWidth={cellW} style:height="{fmt(model.height * scale)}px">
    <div
      bind:this={plane}
      class="ir-card"
      role="img"
      aria-label={model.summary}
      data-form={parsed.clir.form}
      data-span={parsed.clir.span}
      style:width="{fmt(model.width)}px"
      style:height="{fmt(model.height)}px"
      style:transform={scale === 1 ? null : `scale(${fmt(scale)})`}
    >
      <svg
        class="ir-svg"
        width={fmt(model.width)}
        height={fmt(model.height)}
        viewBox="0 0 {fmt(model.width)} {fmt(model.height)}"
        aria-hidden="true"
      >
        {#each model.ops as op, i (i)}
          {@const prim = svgPrim(op)}
          {#if prim}
            {#if prim.k === "rect"}
              {@const r = prim as RectPrim}
              {@const g = boxRect({ x0: r.x0, x1: r.x1, y0: r.y0, y1: r.y1 }, model.width)}
              <rect x={fmt(g.x)} y={fmt(g.y)} width={fmt(g.w)} height={fmt(g.h)} rx={fmt(r.r)}
                style:fill={tokVar(r.tok)}
                style:stroke={r.stroke ? tokVar(r.stroke) : "none"}
                style:opacity={r.alpha ?? 1} />
            {:else if prim.k === "line"}
              {@const l = prim as LinePrim & { _segs?: Array<[number, number]> }}
              <line
                x1={fmt(rx(l.x0, model.width))} y1={fmt(l.y0)}
                x2={fmt(rx(l.x1, model.width))} y2={fmt(l.y1)}
                style:stroke={tokVar(l.tok)}
                style:stroke-width={fmt(l.w)}
                style:stroke-dasharray={l._segs
                  ? gapDash(l._segs, Math.min(l.y0, l.y1), Math.max(l.y0, l.y1))
                  : (l.dash ? l.dash.map(fmt).join(" ") : "none")}
                style:opacity={l.alpha ?? 1} />
            {:else if prim.k === "tri"}
              {@const t = prim as TriPrim}
              <polygon points={triPoints(t, model.width)} style:fill={tokVar(t.tok)} style:opacity={t.alpha ?? 1} />
            {:else if prim.k === "dot"}
              {@const d = prim as DotPrim}
              <g style:opacity={d.alpha ?? 1}>
                {#if d.ring}
                  <circle cx={fmt(rx(d.x, model.width))} cy={fmt(d.y)} r={fmt(d.r + d.ring_w)} style:fill={tokVar(d.ring)} />
                {/if}
                <circle cx={fmt(rx(d.x, model.width))} cy={fmt(d.y)} r={fmt(d.r)} style:fill={tokVar(d.tok)} />
              </g>
            {:else if prim.k === "path"}
              {@const pp = prim as PathPrim}
              {@const dd = pathD(pp, model.width)}
              <g style:opacity={pp.alpha ?? 1}>
                {#if dd.fill && pp.fill}
                  <path d={dd.fill} style:fill={tokVar(pp.fill)} style:stroke="none" />
                {/if}
                <path d={dd.line}
                  style:fill="none"
                  style:stroke={tokVar(pp.tok)}
                  style:stroke-width={fmt(pp.w)}
                  style:stroke-dasharray={pp.style === "interp" ? "4 3" : "none"}
                  style:stroke-linejoin="round"
                  style:stroke-linecap="round" />
              </g>
            {:else if prim.k === "cells"}
              {@const cp = prim as CellsPrim}
              <g style:opacity={cp.alpha ?? 1}>
                {#each cellsFor(cp, model.width) as cell, j (j)}
                  <rect x={fmt(cell.x)} y={fmt(cell.y)} width={fmt(cell.w)} height={fmt(cell.h)} rx="1.5" style={cell.style} />
                {/each}
              </g>
            {:else if prim.k === "basemap"}
              {@const bm = prim as BasemapPrim}
              {@const g = boxRect(bm.box, model.width)}
              {#if world && world.length > 0}
                <!-- The coastlines vector.py paints: the rings touching the bbox, projected
                     into the box (mapPaths mirrors the Python projection) and clipped to it. -->
                <g style:opacity={bm.alpha ?? 1}>
                  <clipPath id="{uid}-map-{bm.id}">
                    <rect x={fmt(g.x)} y={fmt(g.y)} width={fmt(g.w)} height={fmt(g.h)} />
                  </clipPath>
                  <path d={mapPaths(bm, model.width, world).join(" ")}
                    clip-path="url(#{uid}-map-{bm.id})"
                    style:fill={tokVar(bm.land)}
                    style:stroke={tokVar(bm.stroke)}
                    style:stroke-width="0.75"
                    style:stroke-linejoin="round" />
                </g>
              {:else}
                <!-- The land box, as before, until the rings arrive (or if they never do). -->
                <rect x={fmt(g.x)} y={fmt(g.y)} width={fmt(g.w)} height={fmt(g.h)}
                  style:fill={tokVar(bm.land)}
                  style:stroke={tokVar(bm.stroke)}
                  style:stroke-width="0.75"
                  style:opacity={bm.alpha ?? 1} />
              {/if}
            {:else if prim.k === "icon"}
              {@const ic = prim as IconPrim}
              {@const sc = iconScale(ic)}
              <g
                transform="translate({fmt(rx(ic.x, model.width) - 12 * sc)},{fmt(ic.y - 12 * sc)}) scale({fmt(sc)})"
                style:fill="none"
                style:stroke-width={fmt(NI_ICONS.stroke)}
                style:stroke-linecap="round"
                style:stroke-linejoin="round"
                style:opacity={ic.alpha ?? 1}
              >
                {#each (NI_ICONS.icons[ic.name] ?? []) as el, j (j)}
                  {#if el.t === "circle"}
                    <circle cx={fmt(el.c[0])} cy={fmt(el.c[1])} r={fmt(el.r)}
                      style:fill={el.fill ? tokVar(ic.tok) : "none"}
                      style:stroke={el.fill ? "none" : tokVar(ic.tok)} />
                  {:else if el.closed}
                    <polygon points={pointList(el.pts)}
                      style:fill={el.fill ? tokVar(ic.tok) : "none"}
                      style:stroke={el.fill ? "none" : tokVar(ic.tok)} />
                  {:else}
                    <polyline points={pointList(el.pts)} style:fill="none" style:stroke={tokVar(ic.tok)} />
                  {/if}
                {/each}
              </g>
            {/if}
          {/if}
        {/each}
      </svg>

      <!-- Image prims: no shipped form emits one yet (the image form is deferred), so the
           box paints as a quiet placeholder; the loader lands with that form. -->
      {#each model.ops as op, i (i)}
        {@const prim = svgPrim(op)}
        {#if prim && prim.k === "image"}
          {@const im = prim as ImagePrim}
          {@const g = boxRect(im.box, model.width)}
          <div class="ir-img" aria-hidden="true"
            style:left="{fmt(g.x)}px" style:top="{fmt(g.y)}px"
            style:width="{fmt(g.w)}px" style:height="{fmt(g.h)}px"
            style:opacity={im.alpha ?? 1}></div>
        {/if}
      {/each}

      <!-- Text and time prims: real, selectable DOM text positioned on the baseline the
           server computed. Every geometric value is per-prim, hence inline. -->
      {#each model.ops as op, i (i)}
        {#if isText(op)}
          <div
            class="ni-t"
            data-p={op.id}
            data-role={op.role}
            data-max-w={fmt(op.width)}
            dir={op.direction}
            style:left="{fmt(op.left)}px"
            style:top="{fmt(op.top)}px"
            style:width="{fmt(op.width)}px"
            style:font-size="{fmt(op.px)}px"
            style:font-weight={op.wt}
            style:line-height="{fmt(op.lh)}px"
            style:color={tokVar(op.color)}
            style:text-align={op.align}
            style:font-variant-numeric={op.tnum ? "tabular-nums" : "normal"}
            style:opacity={op.alpha}
          >
            {#each op.lines as line, j (j)}
              <span>{line}</span>
            {/each}
          </div>
        {/if}
      {/each}
    </div>
  </div>
{/if}

<style>
  /* The wrapper fills the card body and reports its width; the plane is a positioned
     coordinate system painted at the laid-out width (scaled down only when the cell
     is narrower than the bucket floor). The SVG is one layer; text sits above it as
     absolutely positioned DOM. unicode-bidi: isolate keeps RTL runs from leaking
     direction into neighbours; overflow + ellipsis is the safety net the layout aims
     never to need. */
  .ir-wrap {
    width: 100%;
    overflow: hidden;
  }
  .ir-card {
    position: relative;
    transform-origin: top left;
  }
  .ir-svg {
    position: absolute;
    left: 0;
    top: 0;
    overflow: visible;
    pointer-events: none;
  }
  .ni-t {
    position: absolute;
    white-space: pre;
    overflow: hidden;
    text-overflow: ellipsis;
    unicode-bidi: isolate;
    margin: 0;
  }
  .ni-t > span {
    display: block;
    white-space: pre;
  }
  .ir-img {
    position: absolute;
    border-radius: var(--r-2);
    background: var(--ni-viz-track);
  }
  .ni-bad {
    font-size: var(--f-meta);
    font-style: italic;
  }
</style>
