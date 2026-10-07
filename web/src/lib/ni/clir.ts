// Neural Interface CLIR (Phase 1a-3) — client core. Pure TypeScript, no DOM.
//
// Contract source: app/smartbrain_3000/ni_forms/types.py (CLIR + prim kinds).
// Live binding semantics (apply_live, format_time, countdown/count_up/age, now_marker
// + gap splitting) are a straight port of app/smartbrain_3000/ni_forms/paint/live.py
// — see README-irpaint.md for the parity rule. The client NEVER lints, NEVER lays
// out, NEVER rehashes: it validates the frozen CLIR and paints what the server built.
//
// `renderModel(clir, {width, now, viewerTz})` -> a flat list of positioned
// paint ops; the component maps each op to a <div> or <svg> element. All colours are
// token names resolved to --ni-{token} CSS vars only inside the component.

import {
  NI_TYPE,
  NI_VIZ,
  isTokenName,
  type NiTokenName,
} from "./tokens";

// Inter's vertical metrics (hhea; identical for the four weights under ni_forms/assets/
// fonts). The client places text boxes with the same numbers the Python painter reads
// from the TTFs, so both paint the baseline at the CLIR's `y`.
export const INTER_ASCENT = 0.96875;
export const INTER_DESCENT = 0.2412109375;

export class ClirError extends Error {
  constructor(public code: string, msg: string) {
    super(msg);
    this.name = "ClirError";
  }
}

const FORMS = new Set([
  "stat", "conditions", "kv_grid", "compare", "bars", "series_line", "heatmap",
  "event_curve", "next_event", "agenda", "day_table", "entity_list", "ranked_list",
  "table", "text_brief", "image", "progress", "map_lite",
]);
const TEXT_ROLES = new Set(Object.keys(NI_TYPE));
const TEXT_SRC = new Set(["data", "lexicon", "ask", "title", "key", "code"]);
const TIME_FMTS = new Set([
  "h:mm a", "h a", "ha_short", "EEE", "EEE d", "MMM d", "EEE MMM d", "yyyy",
  "MMM yyyy", "MMM d, yyyy", "MMM d, h:mm a", "HH:mm",
]);
const ICONS = new Set([
  "sun", "moon", "cloud", "cloud_sun", "cloud_moon", "rain", "drizzle", "snow",
  "storm", "fog", "wind", "alert", "check", "cross", "clock", "link_out", "lock",
  "unlock", "pin", "up", "down",
]);
const PRIM_KINDS = new Set([
  "text", "time", "rect", "line", "tri", "dot", "path", "cells",
  "image", "basemap", "icon",
]);
const ANCHORS = new Set(["start", "middle", "end"]);
const TRI_DIRS = new Set(["up", "down", "left", "right"]);

// ------------------------------------------------------------------ anchors + types
export type XAnchor =
  | readonly ["l", number]
  | readonly ["r", number]
  | readonly ["c", number]
  | readonly ["f", number]
  | readonly ["m", XAnchor, XAnchor, number];

export interface Box { x0: XAnchor; x1: XAnchor; y0: number; y1: number }

export interface TextPrim {
  k: "text"; id: number; x: XAnchor; y: number; max_w: number; lines: string[];
  role: string; px: number; tok: NiTokenName; anchor: "start" | "middle" | "end";
  dir: "ltr" | "rtl"; src: string; alpha?: number;
}
export interface TimePrim {
  k: "time"; id: number; x: XAnchor; y: number; max_w: number;
  t: string; fmt: string; tz: "card" | "viewer"; zone: string;
  role: string; px: number; tok: NiTokenName; anchor: "start" | "middle" | "end";
  show_zone: boolean; alpha?: number;
  // apply_live may rewrite a time prim into a text prim via `lines`.
  lines?: string[];
  // Time prims never carry an explicit dir in the CLIR; the painter uses "ltr".
  // Kept optional so textGeom can treat text + time uniformly.
  dir?: "ltr" | "rtl";
}
export interface RectPrim {
  k: "rect"; id: number; x0: XAnchor; x1: XAnchor; y0: number; y1: number;
  tok: NiTokenName; r: number; stroke: NiTokenName | null; alpha?: number;
}
export interface LinePrim {
  k: "line"; id: number; x0: XAnchor; y0: number; x1: XAnchor; y1: number;
  tok: NiTokenName; w: number; dash: number[] | null; alpha?: number;
  gaps?: number[]; // text/time prim ids
}
export interface TriPrim {
  k: "tri"; id: number; x: XAnchor; y: number; size: number;
  dir: "up" | "down" | "left" | "right"; tok: NiTokenName; alpha?: number;
}
export interface DotPrim {
  k: "dot"; id: number; x: XAnchor; y: number; r: number;
  tok: NiTokenName; ring: NiTokenName | null; ring_w: number; alpha?: number;
}
export interface PathPrim {
  k: "path"; id: number; box: Box; pts: Array<[number, number]>;
  tok: NiTokenName; w: number; style: "solid" | "interp";
  fill: NiTokenName | null; alpha?: number;
}
export interface CellsPrim {
  k: "cells"; id: number; box: Box; cols: number; rows: number;
  v: number[]; ramp: "seq" | "div"; steps: number; gap: number; alpha?: number;
}
export interface ImagePrim {
  k: "image"; id: number; box: Box; ref: string; fit: "cover" | "contain";
  crop: [number, number, number, number] | null; alpha?: number;
}
export interface BasemapPrim {
  k: "basemap"; id: number; box: Box; asset: string;
  bbox: [number, number, number, number]; land: NiTokenName; stroke: NiTokenName;
  alpha?: number;
}
export interface IconPrim {
  k: "icon"; id: number; x: XAnchor; y: number; size: number;
  name: string; tok: NiTokenName; alpha?: number;
}
export type Prim =
  | TextPrim | TimePrim | RectPrim | LinePrim | TriPrim | DotPrim
  | PathPrim | CellsPrim | ImagePrim | BasemapPrim | IconPrim;

export interface LiveBinding { k: string; args: Record<string, unknown> }

export interface Clir {
  v: 1; form: string; variant: string; cand: string; plan: string; span: string;
  bucket: { min_w: number; max_w: number; h: number };
  state: string;
  prims: Prim[];
  reading_order: number[];
  summary: string;
  live: LiveBinding[];
  hitmap: Array<{ prim: number; label: string }>;
  dropped: string[]; ladder: string[];
}

// ------------------------------------------------------------------ validation
function fail(code: string, msg: string): never {
  throw new ClirError(code, msg);
}

function requireNumber(x: unknown, where: string, min = -Infinity): number {
  console.assert(typeof where === "string", "requireNumber: where is string");
  console.assert(typeof min === "number", "requireNumber: min is number");
  if (typeof x !== "number" || !Number.isFinite(x) || x < min) {
    fail("bad_number", `${where}: expected finite number >= ${min}, got ${String(x)}`);
  }
  return x;
}

function isXAnchor(x: unknown): x is XAnchor {
  if (!Array.isArray(x) || x.length < 2) return false;
  const k = x[0];
  if (k === "l" || k === "r" || k === "c" || k === "f") {
    return x.length === 2 && typeof x[1] === "number" && Number.isFinite(x[1]);
  }
  if (k === "m") {
    return x.length === 4 && isXAnchor(x[1]) && isXAnchor(x[2])
      && typeof x[3] === "number" && Number.isFinite(x[3]);
  }
  return false;
}

function requireAnchor(x: unknown, where: string): XAnchor {
  console.assert(typeof where === "string", "requireAnchor: where is string");
  console.assert(where.length > 0, "requireAnchor: where non-empty");
  if (!isXAnchor(x)) fail("bad_anchor", `${where}: not a valid X anchor`);
  return x;
}

function requireTok(x: unknown, where: string): NiTokenName {
  console.assert(typeof where === "string", "requireTok: where is string");
  console.assert(where.length > 0, "requireTok: where non-empty");
  if (!isTokenName(x)) fail("bad_token", `${where}: unknown token ${String(x)}`);
  return x;
}

function optionalTok(x: unknown, where: string): NiTokenName | null {
  if (x === null || x === undefined) return null;
  return requireTok(x, where);
}

function requireEnum<T extends string>(
  x: unknown, allowed: Set<string>, where: string,
): T {
  console.assert(typeof where === "string", "requireEnum: where is string");
  console.assert(allowed.size > 0, "requireEnum: allowed non-empty");
  if (typeof x !== "string" || !allowed.has(x)) {
    fail("bad_enum", `${where}: value ${String(x)} not allowed`);
  }
  return x as T;
}

// -------- text / time ---------------------------------------------------------
function validateTextPrim(p: Record<string, unknown>, where: string): TextPrim {
  const role = requireEnum<string>(p.role, TEXT_ROLES, `${where}.role`);
  const px = requireNumber(p.px, `${where}.px`, 11);
  const anchor = requireEnum<"start" | "middle" | "end">(
    p.anchor ?? "start", ANCHORS, `${where}.anchor`,
  );
  const src = requireEnum<string>(p.src ?? "data", TEXT_SRC, `${where}.src`);
  const dir = p.dir === "rtl" ? "rtl" : "ltr";
  const lines = Array.isArray(p.lines) ? p.lines.map(String) : [];
  return {
    k: "text", id: requireNumber(p.id, `${where}.id`),
    x: requireAnchor(p.x, `${where}.x`), y: requireNumber(p.y, `${where}.y`),
    max_w: requireNumber(p.max_w, `${where}.max_w`, 0), lines, role, px,
    tok: requireTok(p.tok, `${where}.tok`), anchor, dir, src,
  };
}

function validateTimePrim(p: Record<string, unknown>, where: string): TimePrim {
  const role = requireEnum<string>(p.role, TEXT_ROLES, `${where}.role`);
  const px = requireNumber(p.px, `${where}.px`, 11);
  const anchor = requireEnum<"start" | "middle" | "end">(
    p.anchor ?? "start", ANCHORS, `${where}.anchor`,
  );
  const fmt = requireEnum<string>(p.fmt, TIME_FMTS, `${where}.fmt`);
  const tz = p.tz === "viewer" ? "viewer" : "card";
  return {
    k: "time", id: requireNumber(p.id, `${where}.id`),
    x: requireAnchor(p.x, `${where}.x`), y: requireNumber(p.y, `${where}.y`),
    max_w: requireNumber(p.max_w, `${where}.max_w`, 0),
    t: String(p.t), fmt, tz, zone: String(p.zone), role, px,
    tok: requireTok(p.tok, `${where}.tok`), anchor,
    show_zone: Boolean(p.show_zone),
  };
}

// -------- shapes --------------------------------------------------------------
function validateRectPrim(p: Record<string, unknown>, where: string): RectPrim {
  return {
    k: "rect", id: requireNumber(p.id, `${where}.id`),
    x0: requireAnchor(p.x0, `${where}.x0`), x1: requireAnchor(p.x1, `${where}.x1`),
    y0: requireNumber(p.y0, `${where}.y0`), y1: requireNumber(p.y1, `${where}.y1`),
    tok: requireTok(p.tok, `${where}.tok`),
    r: typeof p.r === "number" ? p.r : 0,
    stroke: optionalTok(p.stroke ?? null, `${where}.stroke`),
  };
}

function validateLinePrim(p: Record<string, unknown>, where: string): LinePrim {
  return {
    k: "line", id: requireNumber(p.id, `${where}.id`),
    x0: requireAnchor(p.x0, `${where}.x0`), y0: requireNumber(p.y0, `${where}.y0`),
    x1: requireAnchor(p.x1, `${where}.x1`), y1: requireNumber(p.y1, `${where}.y1`),
    tok: requireTok(p.tok, `${where}.tok`),
    w: typeof p.w === "number" ? p.w : 1,
    dash: Array.isArray(p.dash) ? p.dash.map((v) => Number(v)) : null,
    gaps: Array.isArray(p.gaps) ? p.gaps.map((v) => Number(v)) : undefined,
  };
}

function validateTriPrim(p: Record<string, unknown>, where: string): TriPrim {
  return {
    k: "tri", id: requireNumber(p.id, `${where}.id`),
    x: requireAnchor(p.x, `${where}.x`), y: requireNumber(p.y, `${where}.y`),
    size: requireNumber(p.size, `${where}.size`, 0),
    dir: requireEnum<"up" | "down" | "left" | "right">(p.dir, TRI_DIRS, `${where}.dir`),
    tok: requireTok(p.tok, `${where}.tok`),
  };
}

function validateDotPrim(p: Record<string, unknown>, where: string): DotPrim {
  return {
    k: "dot", id: requireNumber(p.id, `${where}.id`),
    x: requireAnchor(p.x, `${where}.x`), y: requireNumber(p.y, `${where}.y`),
    r: requireNumber(p.r, `${where}.r`, 0),
    tok: requireTok(p.tok, `${where}.tok`),
    ring: optionalTok(p.ring ?? null, `${where}.ring`),
    ring_w: typeof p.ring_w === "number" ? p.ring_w : 0,
  };
}

function requireBox(x: unknown, where: string): Box {
  if (!x || typeof x !== "object") fail("bad_box", `${where}: missing`);
  const b = x as Record<string, unknown>;
  return {
    x0: requireAnchor(b.x0, `${where}.x0`), x1: requireAnchor(b.x1, `${where}.x1`),
    y0: requireNumber(b.y0, `${where}.y0`), y1: requireNumber(b.y1, `${where}.y1`),
  };
}

function validatePathPrim(p: Record<string, unknown>, where: string): PathPrim {
  const pts = Array.isArray(p.pts) ? p.pts : [];
  if (pts.length > 400) fail("too_many_points", `${where}.pts > 400`);
  const style = requireEnum<"solid" | "interp">(
    p.style ?? "solid", new Set(["solid", "interp"]), `${where}.style`,
  );
  return {
    k: "path", id: requireNumber(p.id, `${where}.id`),
    box: requireBox(p.box, `${where}.box`),
    pts: pts.map((q) => {
      if (!Array.isArray(q) || q.length !== 2) fail("bad_point", `${where}.pts`);
      return [Number(q[0]), Number(q[1])] as [number, number];
    }),
    tok: requireTok(p.tok, `${where}.tok`),
    w: typeof p.w === "number" ? p.w : 2, style,
    fill: optionalTok(p.fill ?? null, `${where}.fill`),
  };
}

function validateCellsPrim(p: Record<string, unknown>, where: string): CellsPrim {
  const ramp = requireEnum<"seq" | "div">(
    p.ramp ?? "seq", new Set(["seq", "div"]), `${where}.ramp`,
  );
  return {
    k: "cells", id: requireNumber(p.id, `${where}.id`),
    box: requireBox(p.box, `${where}.box`),
    cols: requireNumber(p.cols, `${where}.cols`, 1),
    rows: requireNumber(p.rows, `${where}.rows`, 1),
    v: Array.isArray(p.v) ? p.v.map((q) => (q == null ? -1 : Number(q))) : [],
    ramp, steps: typeof p.steps === "number" ? p.steps : 5,
    gap: typeof p.gap === "number" ? p.gap : 1,
  };
}

function validateImagePrim(p: Record<string, unknown>, where: string): ImagePrim {
  const fit = requireEnum<"cover" | "contain">(
    p.fit ?? "contain", new Set(["cover", "contain"]), `${where}.fit`,
  );
  const crop = Array.isArray(p.crop) && p.crop.length === 4
    ? [Number(p.crop[0]), Number(p.crop[1]), Number(p.crop[2]), Number(p.crop[3])] as [number, number, number, number]
    : null;
  return {
    k: "image", id: requireNumber(p.id, `${where}.id`),
    box: requireBox(p.box, `${where}.box`),
    ref: String(p.ref ?? ""), fit, crop,
  };
}

function validateBasemapPrim(p: Record<string, unknown>, where: string): BasemapPrim {
  const bbox = Array.isArray(p.bbox) && p.bbox.length === 4
    ? [Number(p.bbox[0]), Number(p.bbox[1]), Number(p.bbox[2]), Number(p.bbox[3])] as [number, number, number, number]
    : [0, 0, 0, 0] as [number, number, number, number];
  return {
    k: "basemap", id: requireNumber(p.id, `${where}.id`),
    box: requireBox(p.box, `${where}.box`),
    asset: String(p.asset ?? "world110m"), bbox,
    land: requireTok(p.land ?? "map-land", `${where}.land`),
    stroke: requireTok(p.stroke ?? "map-stroke", `${where}.stroke`),
  };
}

function validateIconPrim(p: Record<string, unknown>, where: string): IconPrim {
  return {
    k: "icon", id: requireNumber(p.id, `${where}.id`),
    x: requireAnchor(p.x, `${where}.x`), y: requireNumber(p.y, `${where}.y`),
    size: requireNumber(p.size, `${where}.size`, 0),
    name: requireEnum<string>(p.name, ICONS, `${where}.name`),
    tok: requireTok(p.tok, `${where}.tok`),
  };
}

const PRIM_VALIDATORS: Record<string, (p: Record<string, unknown>, w: string) => Prim> = {
  text: validateTextPrim, time: validateTimePrim, rect: validateRectPrim,
  line: validateLinePrim, tri: validateTriPrim, dot: validateDotPrim,
  path: validatePathPrim, cells: validateCellsPrim, image: validateImagePrim,
  basemap: validateBasemapPrim, icon: validateIconPrim,
};

function validatePrim(x: unknown, where: string): Prim {
  if (!x || typeof x !== "object") fail("bad_prim", `${where}: not an object`);
  const p = x as Record<string, unknown>;
  const k = p.k;
  if (typeof k !== "string" || !PRIM_KINDS.has(k)) {
    fail("bad_prim_kind", `${where}.k: unknown kind ${String(k)}`);
  }
  return PRIM_VALIDATORS[k](p, where);
}

/** Validate a CLIR dict against the frozen contract. Throws ClirError on bad input. */
export function validateClir(x: unknown): Clir {
  if (!x || typeof x !== "object") fail("bad_clir", "clir: not an object");
  const c = x as Record<string, unknown>;
  if (c.v !== 1) fail("bad_version", "clir.v must be 1");
  const form = requireEnum<string>(c.form, FORMS, "clir.form");
  const prims = Array.isArray(c.prims) ? c.prims : [];
  if (prims.length > 2000) fail("too_many_prims", "clir.prims > 2000");
  const seen = new Set<number>();
  const outPrims: Prim[] = [];
  prims.forEach((p, i) => {
    const prim = validatePrim(p, `clir.prims[${i}]`);
    if (seen.has(prim.id)) fail("dup_prim_id", `clir.prims[${i}]: dup id ${prim.id}`);
    seen.add(prim.id);
    outPrims.push(prim);
  });
  if (typeof c.summary !== "string" || !c.summary) fail("bad_summary", "clir.summary missing");
  const bucket = (c.bucket && typeof c.bucket === "object") ? c.bucket as Record<string, unknown> : {};
  return {
    v: 1, form, variant: String(c.variant ?? ""), cand: String(c.cand ?? ""),
    plan: String(c.plan ?? ""), span: String(c.span ?? ""),
    bucket: {
      min_w: Number(bucket.min_w ?? 0), max_w: Number(bucket.max_w ?? 0),
      h: Number(bucket.h ?? 0),
    },
    state: String(c.state ?? "ok"),
    prims: outPrims,
    reading_order: Array.isArray(c.reading_order) ? c.reading_order.map(Number) : [],
    summary: c.summary,
    live: Array.isArray(c.live) ? c.live.map((lb) => ({
      k: String((lb as Record<string, unknown>).k ?? ""),
      args: ((lb as Record<string, unknown>).args ?? {}) as Record<string, unknown>,
    })) : [],
    hitmap: Array.isArray(c.hitmap) ? c.hitmap.map((h) => ({
      prim: Number((h as Record<string, unknown>).prim ?? -1),
      label: String((h as Record<string, unknown>).label ?? ""),
    })) : [],
    dropped: Array.isArray(c.dropped) ? c.dropped.map(String) : [],
    ladder: Array.isArray(c.ladder) ? c.ladder.map(String) : [],
  };
}

// ------------------------------------------------------------------ anchors
export function resolveX(x: XAnchor, left: number, width: number): number {
  console.assert(typeof left === "number", "resolveX: left is number");
  console.assert(typeof width === "number", "resolveX: width is number");
  if (x[0] === "l") return left + x[1];
  if (x[0] === "r") return left + width - x[1];
  if (x[0] === "c") return left + width / 2 + x[1];
  if (x[0] === "f") return left + x[1] * width;
  // "m": a frac between two sub-anchors — emitted by apply_live when it moves a prim.
  const a = resolveX(x[1], left, width);
  const b = resolveX(x[2], left, width);
  return a + (b - a) * x[3];
}

function lerpAnchor(a: XAnchor, b: XAnchor, frac: number): XAnchor {
  console.assert(Number.isFinite(frac), "lerpAnchor: frac finite");
  console.assert(Array.isArray(a) && Array.isArray(b), "lerpAnchor: anchors arrays");
  // Both ends are a simple-anchor "l/c/f/r" with a numeric offset; mirror live.py
  // exactly. Only the "m" (sub-anchor) kind would make a[1] non-numeric — never
  // reached here because apply_live never nests "m" inside itself.
  const simple = (k: XAnchor[0]): k is "l" | "r" | "c" | "f" =>
    k === "l" || k === "r" || k === "c" || k === "f";
  if (simple(a[0]) && simple(b[0]) && a[0] === b[0] && a[0] !== "r") {
    const ao = a[1] as number, bo = b[1] as number;
    return [a[0], ao + (bo - ao) * frac] as XAnchor;
  }
  if (a[0] === "r" && b[0] === "r") {
    const ao = a[1] as number, bo = b[1] as number;
    return ["r", ao + (bo - ao) * frac] as XAnchor;
  }
  if (a[0] === "l" && b[0] === "r" && (a[1] as number) === 0 && (b[1] as number) === 0) {
    return ["f", frac] as XAnchor;
  }
  return ["m", a, b, frac] as XAnchor;
}

// ------------------------------------------------------------------ time
function parseT(t: string): number | { date: true; y: number; m: number; d: number } {
  console.assert(typeof t === "string", "parseT: t is string");
  console.assert(t.length > 0, "parseT: t non-empty");
  if (t.length === 10) {
    const [y, m, d] = t.split("-").map(Number);
    return { date: true, y, m: m - 1, d };
  }
  return Date.parse(t);
}

/** Milliseconds-since-epoch for a parsed time, interpreting a floating date in UTC. */
function tMillis(v: ReturnType<typeof parseT>): number {
  if (typeof v === "number") return v;
  return Date.UTC(v.y, v.m, v.d);
}

const FMT_PARTS = (zone: string) => new Intl.DateTimeFormat("en-US", {
  timeZone: zone, hour: "numeric", minute: "2-digit", second: "2-digit",
  weekday: "short", month: "short", day: "numeric", year: "numeric",
  hour12: false, hourCycle: "h23",
});

function partsOf(ms: number, zone: string): Record<string, string> {
  const parts = FMT_PARTS(zone).formatToParts(new Date(ms));
  const out: Record<string, string> = {};
  for (const p of parts) if (p.type !== "literal") out[p.type] = p.value;
  return out;
}

function zoneAbbr(ms: number, zone: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: zone, timeZoneName: "short", year: "numeric",
  }).formatToParts(new Date(ms));
  const z = parts.find((p) => p.type === "timeZoneName")?.value ?? "";
  if (z.startsWith("GMT") || z.startsWith("UTC")) {
    return z.replace("GMT", "UTC").replace("UTC+0", "UTC").replace("UTC-0", "UTC");
  }
  return z;
}

/** Format an absolute time in `zone`. Date-only values are floating (no shift). */
export function formatTime(
  tIso: string, fmt: string, zone: string, showZone: boolean,
): string {
  const v = parseT(tIso);
  const dateOnly = typeof v !== "number";
  const ms = tMillis(v);
  const p = partsOf(ms, dateOnly ? "UTC" : zone);
  const h24 = Number(p.hour), h12 = (h24 % 12) || 12;
  const ampm = h24 < 12 ? "am" : "pm";
  const mm = p.minute, hh = h24.toString().padStart(2, "0");
  const dayN = Number(p.day);
  const abbrevs: Record<string, string> = {
    "h:mm a": `${h12}:${mm} ${ampm}`,
    "h a": `${h12} ${ampm}`,
    "ha_short": `${h12}${ampm[0]}`,
    "EEE": p.weekday,
    "EEE d": `${p.weekday} ${dayN}`,
    "MMM d": `${p.month} ${dayN}`,
    "EEE MMM d": `${p.weekday} ${p.month} ${dayN}`,
    "yyyy": p.year,
    "MMM yyyy": `${p.month} ${p.year}`,
    "MMM d, yyyy": `${p.month} ${dayN}, ${p.year}`,
    "MMM d, h:mm a": `${p.month} ${dayN}, ${h12}:${mm} ${ampm}`,
    "HH:mm": `${hh}:${mm}`,
  };
  const s = abbrevs[fmt];
  if (s === undefined) throw new ClirError("bad_fmt", `formatTime: unknown fmt ${fmt}`);
  return showZone && !dateOnly ? `${s} ${zoneAbbr(ms, zone)}` : s;
}

export function timePrimText(p: TimePrim, viewerTz: string): string {
  console.assert(typeof viewerTz === "string", "timePrimText: viewerTz is string");
  console.assert(p.k === "time", "timePrimText: prim is time");
  const zone = p.tz === "card" ? p.zone : viewerTz;
  const show = p.show_zone && zone !== viewerTz;
  return formatTime(p.t, p.fmt, zone, show);
}

// ------------------------------------------------------------------ countdown etc
export function countdownText(seconds: number, fmt = "in_hm"): string {
  console.assert(Number.isFinite(seconds), "countdownText: seconds finite");
  console.assert(typeof fmt === "string", "countdownText: fmt is string");
  const s = Math.trunc(seconds);
  if (s <= 0) return "now";
  const mt = Math.floor((s + 59) / 60);
  const d = Math.floor(mt / 1440);
  const rem = mt % 1440;
  let h = Math.floor(rem / 60);
  const m = rem % 60;
  if (fmt === "rel" || (fmt === "in_dhm" && d >= 2)) {
    if (d >= 2) return `in ${d} days`;
    if (d === 1) return h === 0 ? "in 1 day" : `in 1 d ${h} h`;
  }
  h += d * 24;
  const core = (h && m) ? `${h} h ${m} m` : h ? `${h} h` : `${m} min`;
  return fmt === "hm" ? core : `in ${core}`;
}

export function countUpText(seconds: number, unit: "auto" | "day" | "hour" | "minute" = "auto"): string {
  console.assert(Number.isFinite(seconds), "countUpText: seconds finite");
  console.assert(typeof unit === "string", "countUpText: unit is string");
  const s = Math.max(0, Math.trunc(seconds));
  const u = unit === "auto" ? (s >= 2 * 86400 ? "day" : s >= 2 * 3600 ? "hour" : "minute") : unit;
  const n = Math.floor(s / ({ day: 86400, hour: 3600, minute: 60 } as const)[u]);
  const word = { day: "day", hour: "hour", minute: "minute" }[u];
  return `${n.toLocaleString("en-US")} ${word}${n === 1 ? "" : "s"}`;
}

export function ageText(seconds: number): string {
  console.assert(Number.isFinite(seconds), "ageText: seconds finite");
  console.assert(typeof seconds === "number", "ageText: seconds number");
  const s = Math.max(0, Math.trunc(seconds));
  if (s < 3600) return `${Math.max(1, Math.floor(s / 60))} min old`;
  if (s < 2 * 86400) return `${Math.floor(s / 3600)} h old`;
  return `${Math.floor(s / 86400)} d old`;
}

const NUM_RE = /^(,?)(?:\.(\d)([f%])|(d))$/;
export function numberText(v: number, fmt: string): string {
  console.assert(Number.isFinite(v), "numberText: v finite");
  console.assert(typeof fmt === "string", "numberText: fmt is string");
  const m = NUM_RE.exec(fmt) ?? [null, ",", "0", "f"];
  const grp = m[1] === ",";
  const asPct = m[3] === "%";
  const asInt = m[4] === "d";
  const dp = asInt ? 0 : (m[2] === undefined ? 0 : Number(m[2]));
  const base = asPct ? v * 100 : v;
  const s = base.toLocaleString("en-US", {
    useGrouping: grp, minimumFractionDigits: dp, maximumFractionDigits: dp,
  });
  return asPct ? `${s}%` : s;
}

// ------------------------------------------------------------------ apply_live
export function activeVariant(variants: Array<{ t_from?: string | null; t_to?: string | null }>, now: number): number {
  console.assert(Array.isArray(variants), "activeVariant: variants array");
  console.assert(Number.isFinite(now), "activeVariant: now finite");
  let best = -1;
  for (let i = 0; i < variants.length; i++) {
    const vf = variants[i].t_from ? tMillis(parseT(variants[i].t_from as string)) : null;
    const vt = variants[i].t_to ? tMillis(parseT(variants[i].t_to as string)) : null;
    if ((vf === null || now >= vf) && (vt === null || now < vt)) return i;
    if (vf === null || now >= vf) best = i;
  }
  return best < 0 ? 0 : best;
}

function pathY(path: PathPrim, fx: number): number | null {
  const pts = path.pts;
  if (!pts.length) return null;
  const b = path.box;
  let fy = pts[0][1];
  if (fx >= pts[pts.length - 1][0]) fy = pts[pts.length - 1][1];
  else if (fx > pts[0][0]) {
    for (let i = 1; i < pts.length; i++) {
      const [x0, y0] = pts[i - 1];
      const [x1, y1] = pts[i];
      if (fx >= x0 && fx <= x1) {
        fy = x1 === x0 ? y0 : y0 + (y1 - y0) * (fx - x0) / (x1 - x0);
        break;
      }
    }
  }
  return b.y0 + fy * (b.y1 - b.y0);
}

/** Pure port of live.apply_live. Resolves every binding at `now` -> a static CLIR
 *  (live=[]). Hidden prims are removed; dimmed prims receive `.alpha`. Never mutates. */
export function applyLive(clir: Clir, now: number): Clir {
  console.assert(Number.isFinite(now), "applyLive: now finite");
  console.assert(clir.v === 1, "applyLive: clir v=1");
  const out: Clir = structuredClone(clir);
  const by = new Map(out.prims.map((p) => [p.id, p]));
  const hidden = new Set<number>();
  for (const lb of out.live) {
    runBinding(lb, by, hidden, now);
  }
  out.prims = out.prims.filter((p) => !hidden.has(p.id));
  out.reading_order = out.reading_order.filter((i) => !hidden.has(i));
  out.live = [];
  return out;
}

function runBinding(
  lb: LiveBinding, by: Map<number, Prim>, hidden: Set<number>, now: number,
): void {
  console.assert(typeof lb.k === "string", "runBinding: k is string");
  console.assert(by instanceof Map, "runBinding: by is Map");
  const a = lb.args;
  if (lb.k === "now_marker") return runNowMarker(a, by, hidden, now);
  if (lb.k === "countdown") return runCountdown(a, by, now);
  if (lb.k === "count_up") return runCountUp(a, by, now);
  if (lb.k === "age") return runAge(a, by, now);
  if (lb.k === "extrapolate") return runExtrapolate(a, by, now);
  if (lb.k === "timed_variants") return runTimedVariants(a, hidden, now);
  if (lb.k === "past_dim") return runPastDim(a, by, now);
}

function runNowMarker(
  a: Record<string, unknown>, by: Map<number, Prim>, hidden: Set<number>, now: number,
): void {
  const box = a.box as Box;
  const t0 = tMillis(parseT(a.t0 as string));
  const t1 = tMillis(parseT(a.t1 as string));
  const span = t1 - t0;
  const frac = span > 0 ? (now - t0) / span : -1;
  const ids = (a.prims as number[] | undefined) ?? [];
  if (!(frac >= 0 && frac <= 1)) {
    for (const i of ids) hidden.add(i);
    return;
  }
  const x = lerpAnchor(box.x0, box.x1, frac);
  const pathPrim = typeof a.path === "number" ? by.get(a.path) : undefined;
  const py = (pathPrim && pathPrim.k === "path") ? pathY(pathPrim, frac) : null;
  for (const id of ids) {
    const p = by.get(id);
    if (!p) continue;
    if (p.k === "line") {
      p.x0 = x; p.x1 = x;
      if (Array.isArray(a.gaps)) (p as LinePrim).gaps = a.gaps as number[];
    } else if (p.k === "dot" || p.k === "tri" || p.k === "icon") {
      p.x = x;
      if (py !== null && p.k === "dot") p.y = py;
    } else if (p.k === "text" || p.k === "time") {
      p.x = x;
    }
  }
}

function runCountdown(a: Record<string, unknown>, by: Map<number, Prim>, now: number): void {
  const p = by.get(a.prim as number);
  if (!p || (p.k !== "text" && p.k !== "time")) return;
  const t = tMillis(parseT(a.t as string));
  p.lines = [countdownText((t - now) / 1000, (a.fmt as string) || "in_hm")];
}

function runCountUp(a: Record<string, unknown>, by: Map<number, Prim>, now: number): void {
  const p = by.get(a.prim as number);
  if (!p || (p.k !== "text" && p.k !== "time")) return;
  const t0 = tMillis(parseT(a.t0 as string));
  const unit = ((a.unit as string) || "auto") as "auto" | "day" | "hour" | "minute";
  p.lines = [countUpText((now - t0) / 1000, unit)];
}

function runAge(a: Record<string, unknown>, by: Map<number, Prim>, now: number): void {
  const p = by.get(a.prim as number);
  if (!p || (p.k !== "text" && p.k !== "time")) return;
  const t = tMillis(parseT(a.t as string));
  p.lines = [ageText((now - t) / 1000)];
}

function runExtrapolate(a: Record<string, unknown>, by: Map<number, Prim>, now: number): void {
  const p = by.get(a.prim as number);
  if (!p || (p.k !== "text" && p.k !== "time")) return;
  const t0 = tMillis(parseT(a.t0 as string));
  const v = (a.v0 as number) + (a.rate_per_s as number) * (now - t0) / 1000;
  p.lines = [numberText(v, (a.fmt as string) || ",.0f")];
}

function runTimedVariants(a: Record<string, unknown>, hidden: Set<number>, now: number): void {
  const vs = (a.variants as Array<{ t_from?: string | null; t_to?: string | null; prims?: number[] }>) ?? [];
  if (!vs.length) return;
  const act = activeVariant(vs, now);
  for (let i = 0; i < vs.length; i++) {
    if (i !== act) for (const pid of vs[i].prims ?? []) hidden.add(pid);
  }
}

function runPastDim(a: Record<string, unknown>, by: Map<number, Prim>, now: number): void {
  const t = tMillis(parseT(a.t as string));
  if (now < t) return;
  for (const id of ((a.prims as number[] | undefined) ?? [])) {
    const p = by.get(id);
    if (p) p.alpha = NI_VIZ.past_alpha;
  }
}

// ------------------------------------------------------------------ render model
export interface RenderText {
  kind: "text";
  id: number; px: number; wt: number; lh: number; tnum: boolean;
  left: number; top: number; width: number;
  color: NiTokenName; align: "left" | "center" | "right"; direction: "ltr" | "rtl";
  lines: string[]; alpha: number; role: string;
}
export type RenderOp =
  | RenderText
  | { kind: "svg"; id: number; prim: Prim };

export interface RenderModel {
  width: number;
  height: number;
  summary: string;
  readingOrder: number[];
  hitmap: Array<{ prim: number; label: string }>;
  ops: RenderOp[];
}

function textRole(role: string): { px: number; wt: number; lh: number; tnum: boolean } {
  const r = NI_TYPE[role];
  console.assert(!!r, "textRole: role exists");
  console.assert(typeof r.px === "number", "textRole: px number");
  return { px: r.px, wt: r.wt, lh: r.lh, tnum: !!r.tnum };
}

function textGeom(p: TextPrim | TimePrim, width: number): RenderText {
  const r = textRole(p.role);
  const px = p.px || r.px;
  const lh = r.lh * px / r.px;
  const x = resolveX(p.x, 0, width);
  const anc = p.anchor;
  const rtl = p.dir === "rtl";
  const phys = rtl
    ? (anc === "start" ? "end" : anc === "end" ? "start" : "middle")
    : anc;
  const left = phys === "start" ? x : phys === "middle" ? x - p.max_w / 2 : x - p.max_w;
  const align = phys === "start" ? "left" : phys === "middle" ? "center" : "right";
  // Same box top as the Python painter (paint/vector.py `_text_div`): the browser centres
  // the font's ascent+descent inside the line box, so top = baseline − half-leading − ascent.
  const top = p.y - (lh - (INTER_ASCENT + INTER_DESCENT) * px) / 2 - INTER_ASCENT * px;
  return {
    kind: "text", id: p.id, px, wt: r.wt, lh, tnum: r.tnum,
    left, top, width: p.max_w, color: p.tok,
    align, direction: p.dir ?? "ltr",
    lines: p.k === "text" ? p.lines : (p.lines ?? []),
    alpha: p.alpha ?? 1, role: p.role,
  };
}

/** Approximate rendered box of a text/time prim, used for line_segments gap splitting.
 *  The Python painter measures the glyph run (fontTools advances); the client has no
 *  glyph widths, so it uses the frozen `max_w` + the role's line-height. The split is
 *  the same even when the pixel bounds differ by a few px: a chart label occupies the
 *  width it was laid out for. */
export function approxTextBox(
  p: TextPrim | TimePrim, width: number,
): { x0: number; y0: number; x1: number; y1: number } {
  const g = textGeom(p, width);
  return { x0: g.left, y0: g.top, x1: g.left + g.width, y1: g.top + g.lh };
}

/** Mirror of live.line_segments: the visible y-ranges of a vertical line whose `gaps`
 *  name text/time prim ids it must not cross. */
export function lineSegments(
  p: LinePrim, byId: Map<number, Prim>, width: number, pad = 3,
): Array<[number, number]> {
  console.assert(byId instanceof Map, "lineSegments: byId is Map");
  console.assert(typeof width === "number", "lineSegments: width is number");
  const x = resolveX(p.x0, 0, width);
  const ya = Math.min(p.y0, p.y1);
  const yb = Math.max(p.y0, p.y1);
  const cuts: Array<[number, number]> = [];
  for (const id of p.gaps ?? []) {
    const q = byId.get(id);
    if (!q || (q.k !== "text" && q.k !== "time")) continue;
    const b = approxTextBox(q, width);
    if (b.x0 - pad <= x && x <= b.x1 + pad) cuts.push([b.y0 - 2, b.y1 + 2]);
  }
  cuts.sort((a, b) => a[0] - b[0]);
  let segs: Array<[number, number]> = [[ya, yb]];
  for (const [c0, c1] of cuts) {
    const next: Array<[number, number]> = [];
    for (const [s0, s1] of segs) {
      if (c1 <= s0 || c0 >= s1) { next.push([s0, s1]); continue; }
      if (c0 > s0) next.push([s0, c0]);
      if (c1 < s1) next.push([c1, s1]);
    }
    segs = next;
  }
  return segs.filter(([a, b]) => b - a > 1);
}

export interface RenderOpts { width: number; now: number; viewerTz: string }

/** Resolve live bindings at `now` and emit a flat list of positioned paint ops. */
export function renderModel(clir: Clir, opts: RenderOpts): RenderModel {
  console.assert(opts.width > 0, "renderModel: width > 0");
  console.assert(Number.isFinite(opts.now), "renderModel: now finite");
  const applied = applyLive(clir, opts.now);
  const byId = new Map(applied.prims.map((p) => [p.id, p]));
  const ops: RenderOp[] = [];
  for (const p of applied.prims) {
    if (p.k === "text") {
      ops.push(textGeom(p, opts.width));
    } else if (p.k === "time") {
      // apply_live may have already resolved a time prim's text (variants slot).
      // Otherwise format the ISO instant per `tz`.
      const lines = p.lines ?? [timePrimText(p, opts.viewerTz)];
      const resolved: TimePrim = { ...p, lines };
      ops.push(textGeom(resolved, opts.width));
    } else {
      ops.push({ kind: "svg", id: p.id, prim: p });
    }
  }
  // Pass known line_segments geometry to the component via the prim itself (annotation).
  for (const p of applied.prims) {
    if (p.k === "line" && p.gaps && p.gaps.length) {
      const segs = lineSegments(p, byId, opts.width);
      (p as LinePrim & { _segs?: Array<[number, number]> })._segs = segs;
    }
  }
  return {
    width: opts.width, height: clir.bucket.h, summary: applied.summary,
    readingOrder: applied.reading_order, hitmap: applied.hitmap, ops,
  };
}
