// Pure geometry helpers for the v2 spark + gauge scene nodes (ni-format §5,
// "Added in v2"). The Svelte component owns markup; this module owns the math so
// it can be unit-tested without a DOM.
//
// Every input has already passed `validateBoundScene` (a spark's points are
// finite numbers or {t, v} objects; a gauge's min/max/value are finite with
// max > min). These helpers still guard degenerate inputs — an empty series
// returns an empty path so the component can show a muted em-dash.

/** Fixed 240° sweep with the gap centered at the bottom (§ prompt). Start at
 *  150° (7-o'clock in SVG's Y-down space) and sweep clockwise to 30°. */
const GAUGE_START_DEG = 150;
const GAUGE_SWEEP_DEG = 240;

export type SparkInput = number | { v: number; t?: string };
export interface BarRect { x: number; y: number; w: number; h: number }
export interface GaugeArc { d: string; pct: number }

function extractV(p: SparkInput): number {
  console.assert(p !== undefined && p !== null, "extractV: point defined");
  console.assert(typeof p === "number" || typeof p === "object", "extractV: number or object");
  return typeof p === "number" ? p : p.v;
}

function seriesRange(points: ReadonlyArray<SparkInput>): { vmin: number; vmax: number } {
  console.assert(Array.isArray(points), "seriesRange: array required");
  console.assert(points.length > 0, "seriesRange: non-empty");
  let vmin = Number.POSITIVE_INFINITY;
  let vmax = Number.NEGATIVE_INFINITY;
  for (const p of points) {
    const v = extractV(p);
    if (v < vmin) vmin = v;
    if (v > vmax) vmax = v;
  }
  return { vmin, vmax };
}

/** 0..1 fraction of `v` within the series range; a constant series maps to 0.5
 *  (the midline convention — the component's job is to keep the trace visible,
 *  not to spike it to an arbitrary edge). */
function normalized(v: number, vmin: number, vmax: number): number {
  console.assert(Number.isFinite(v), "normalized: v finite");
  console.assert(vmax >= vmin, "normalized: max >= min");
  if (vmax === vmin) return 0.5;
  return (v - vmin) / (vmax - vmin);
}

/** Polyline `points` attribute string ("x,y x,y …") over the value series,
 *  scaled to fill (width × height). SVG Y is flipped, so higher values sit at
 *  the top. Two-point series still renders (endpoints span the full width).
 *  Single point sits at the horizontal center on the midline. */
export function sparkPath(
  points: ReadonlyArray<SparkInput>,
  width: number,
  height: number,
): string {
  console.assert(Array.isArray(points), "sparkPath: array required");
  console.assert(width > 0 && height > 0, "sparkPath: positive dims");
  const n = points.length;
  if (n === 0) return "";
  const { vmin, vmax } = seriesRange(points);
  const parts: string[] = [];
  const denom = n === 1 ? 1 : n - 1;
  for (let i = 0; i < n; i += 1) {
    const x = n === 1 ? width / 2 : (i / denom) * width;
    const t = normalized(extractV(points[i]), vmin, vmax);
    const y = height - t * height;
    parts.push(`${x.toFixed(2)},${y.toFixed(2)}`);
  }
  return parts.join(" ");
}

/** Rects for a bar sparkline. Bar widths partition the container evenly (their
 *  sum is `width`); heights come from the same normalization the line path
 *  uses, so a constant series produces mid-height bars, matching the line's
 *  midline convention. Empty → []. */
export function sparkBars(
  points: ReadonlyArray<SparkInput>,
  width: number,
  height: number,
): BarRect[] {
  console.assert(Array.isArray(points), "sparkBars: array required");
  console.assert(width > 0 && height > 0, "sparkBars: positive dims");
  const n = points.length;
  if (n === 0) return [];
  const { vmin, vmax } = seriesRange(points);
  const w = width / n;
  const rects: BarRect[] = [];
  for (let i = 0; i < n; i += 1) {
    const t = normalized(extractV(points[i]), vmin, vmax);
    const h = t * height;
    const y = height - h;
    rects.push({ x: i * w, y, w, h });
  }
  return rects;
}

function polar(r: number, degrees: number): { x: number; y: number } {
  console.assert(Number.isFinite(r) && r > 0, "polar: r > 0");
  console.assert(Number.isFinite(degrees), "polar: degrees finite");
  const rad = (degrees * Math.PI) / 180;
  return { x: r * Math.cos(rad), y: r * Math.sin(rad) };
}

/** SVG arc path centered at (0, 0), radius `r`, for a 240° gauge (§ prompt).
 *  `value` is clamped to [min, max] — clamping is presentation, callers keep
 *  the raw value for aria-valuenow etc. At pct 0 the path is just a `M`
 *  (no `A`) so no arc renders. */
export function gaugeArc(value: number, min: number, max: number, r: number): GaugeArc {
  console.assert(Number.isFinite(value), "gaugeArc: value finite");
  console.assert(max > min, "gaugeArc: max > min");
  const clamped = Math.min(max, Math.max(min, value));
  const pct = (clamped - min) / (max - min);
  const sweep = pct * GAUGE_SWEEP_DEG;
  const start = polar(r, GAUGE_START_DEG);
  const end = polar(r, GAUGE_START_DEG + sweep);
  if (pct === 0) return { d: `M ${start.x.toFixed(2)} ${start.y.toFixed(2)}`, pct };
  const largeArc = sweep > 180 ? 1 : 0;
  const d = `M ${start.x.toFixed(2)} ${start.y.toFixed(2)} A ${r} ${r} 0 ${largeArc} 1 ${end.x.toFixed(2)} ${end.y.toFixed(2)}`;
  return { d, pct };
}
