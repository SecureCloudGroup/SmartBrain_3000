// Neural Interface scene grammar (v1) — TypeScript mirror of docs/internal/ni-format.md
// §5. By render time the server has already bound the scene (every {"$bind": …} and
// {{path}} interpolation resolved to a JSON literal, every repeat expanded), so this
// module never resolves paths — it TYPES the bound tree and REFUSES anything the
// server's validator would have refused. Nothing renders that did not validate.

/** Text roles map to the type-scale tokens; see NiScene.svelte for the mapping. */
export type TextRole = "title" | "label" | "value" | "caption";
export type Tone = "default" | "muted" | "accent" | "ok" | "warn" | "danger";
export type Size = "sm" | "md" | "lg";
export type ChipKind = "" | "accent" | "ok" | "warn" | "danger";
export type StackDir = "v" | "h";
export type StackGap = "sm" | "md";
export type NumberFormat = "plain" | "compact" | "percent" | "currency";
export type SparkKind = "line" | "bars";
export interface SparkPoint { t: string; v: number }

export interface StackNode {
  type: "stack";
  dir: StackDir;
  gap: StackGap;
  children: SceneNode[];
}
export interface GridNode {
  type: "grid";
  cols: 2 | 3 | 4;
  children: SceneNode[];
}
export interface DividerNode { type: "divider" }
export interface TextNode {
  type: "text";
  value: string;
  role: TextRole;
  tone: Tone;
  size: Size;
}
export interface NumberNode {
  type: "number";
  value: number;
  format: NumberFormat;
  unit?: string;
  tone: Tone;
  size: Size;
}
export interface ChipNode { type: "chip"; value: string; kind: ChipKind }
export interface BarNode { type: "bar"; value: number; max: number; tone: Tone }
export interface IconNode { type: "icon"; name: string; tone: Tone }

/** Only used by the pre-bind server-side spec; the renderer REJECTS a surviving one. */
export interface RepeatNode {
  type: "repeat";
  items: unknown;
  max: number;
  template: SceneNode;
}
// v2 nodes — server-side binder resolves any {"$bind": …} into concrete data before render.
export interface SparkNode {
  type: "spark";
  points: Array<number | SparkPoint>;
  kind: SparkKind;
  tone?: Tone;
}
export interface GaugeNode {
  type: "gauge";
  value: number;
  min: number;
  max: number;
  tone?: Tone;
  label?: string;
}

export type SceneNode =
  | StackNode
  | GridNode
  | DividerNode
  | TextNode
  | NumberNode
  | ChipNode
  | BarNode
  | IconNode
  | RepeatNode
  | SparkNode
  | GaugeNode;

// Caps — mirror §5 exactly. A surviving repeat is invalid at render time (§4.3).
const MAX_NODES = 100;
const MAX_DEPTH = 8;
const MAX_TEXT_CHARS = 2000;
const MAX_SPARK_POINTS = 500;
const MAX_LABEL_CHARS = 200;

// Closed enums.
const TEXT_ROLES: readonly TextRole[] = ["title", "label", "value", "caption"];
const TONES: readonly Tone[] = ["default", "muted", "accent", "ok", "warn", "danger"];
const SIZES: readonly Size[] = ["sm", "md", "lg"];
const CHIP_KINDS: readonly ChipKind[] = ["", "accent", "ok", "warn", "danger"];
const STACK_DIRS: readonly StackDir[] = ["v", "h"];
const STACK_GAPS: readonly StackGap[] = ["sm", "md"];
const NUMBER_FORMATS: readonly NumberFormat[] = ["plain", "compact", "percent", "currency"];
const SPARK_KINDS: readonly SparkKind[] = ["line", "bars"];

// Reserved-for-later types — MUST be refused so old clients don't mis-render new scenes.
// `spark` and `gauge` graduated in v2 and are handled below. `when` also stays here so
// {type: "when"} is refused with the same message (the `when` KEY on any node is
// caught earlier by the surviving-key check — server strips it at bind time).
const RESERVED_TYPES: ReadonlySet<string> = new Set(["image", "when", "on_tap"]);

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function checkText(n: Record<string, unknown>): string | null {
  console.assert(n.type === "text", "checkText: type must be text");
  console.assert(typeof n === "object", "checkText: node must be an object");
  if (typeof n.value !== "string") return "text.value must be string";
  if (n.value.length > MAX_TEXT_CHARS) return "text.value exceeds 2000 chars";
  if (!TEXT_ROLES.includes(n.role as TextRole)) return "text.role invalid";
  if (!TONES.includes(n.tone as Tone)) return "text.tone invalid";
  if (!SIZES.includes(n.size as Size)) return "text.size invalid";
  return null;
}

function checkNumber(n: Record<string, unknown>): string | null {
  console.assert(n.type === "number", "checkNumber: type must be number");
  console.assert(typeof n === "object", "checkNumber: node must be an object");
  if (typeof n.value !== "number" || !Number.isFinite(n.value)) return "number.value must be finite";
  if (!NUMBER_FORMATS.includes(n.format as NumberFormat)) return "number.format invalid";
  if (!TONES.includes(n.tone as Tone)) return "number.tone invalid";
  if (!SIZES.includes(n.size as Size)) return "number.size invalid";
  if (n.unit !== undefined && typeof n.unit !== "string") return "number.unit must be string";
  return null;
}

function checkChip(n: Record<string, unknown>): string | null {
  console.assert(n.type === "chip", "checkChip: type must be chip");
  console.assert(typeof n === "object", "checkChip: node must be an object");
  if (typeof n.value !== "string") return "chip.value must be string";
  if (n.value.length > MAX_TEXT_CHARS) return "chip.value exceeds 2000 chars";
  if (!CHIP_KINDS.includes(n.kind as ChipKind)) return "chip.kind invalid";
  return null;
}

function checkBar(n: Record<string, unknown>): string | null {
  console.assert(n.type === "bar", "checkBar: type must be bar");
  console.assert(typeof n === "object", "checkBar: node must be an object");
  if (typeof n.value !== "number" || !Number.isFinite(n.value)) return "bar.value must be finite";
  if (typeof n.max !== "number" || !Number.isFinite(n.max) || n.max <= 0) return "bar.max must be > 0";
  if (!TONES.includes(n.tone as Tone)) return "bar.tone invalid";
  return null;
}

function checkIcon(n: Record<string, unknown>): string | null {
  console.assert(n.type === "icon", "checkIcon: type must be icon");
  console.assert(typeof n === "object", "checkIcon: node must be an object");
  if (typeof n.name !== "string" || n.name.length === 0) return "icon.name must be non-empty string";
  if (!TONES.includes(n.tone as Tone)) return "icon.tone invalid";
  return null;
}

function checkSpark(n: Record<string, unknown>): string | null {
  console.assert(n.type === "spark", "checkSpark: type must be spark");
  console.assert(typeof n === "object", "checkSpark: node must be an object");
  const points = n.points;
  if (!Array.isArray(points)) return "spark.points must be array";
  if (points.length > MAX_SPARK_POINTS) return "spark.points exceeds 500";
  for (const p of points) {
    if (typeof p === "number") {
      if (!Number.isFinite(p)) return "spark.points value must be finite";
      continue;
    }
    if (!isRecord(p)) return "spark.points entry must be number or {t, v}";
    if (typeof p.v !== "number" || !Number.isFinite(p.v)) return "spark.points.v must be finite";
    if (p.t !== undefined && typeof p.t !== "string") return "spark.points.t must be string";
  }
  if (!SPARK_KINDS.includes(n.kind as SparkKind)) return "spark.kind invalid";
  if (n.tone !== undefined && !TONES.includes(n.tone as Tone)) return "spark.tone invalid";
  return null;
}

function checkGauge(n: Record<string, unknown>): string | null {
  console.assert(n.type === "gauge", "checkGauge: type must be gauge");
  console.assert(typeof n === "object", "checkGauge: node must be an object");
  if (typeof n.value !== "number" || !Number.isFinite(n.value)) return "gauge.value must be finite";
  if (typeof n.min !== "number" || !Number.isFinite(n.min)) return "gauge.min must be finite";
  if (typeof n.max !== "number" || !Number.isFinite(n.max)) return "gauge.max must be finite";
  if (n.max <= n.min) return "gauge.max must be > gauge.min";
  if (n.tone !== undefined && !TONES.includes(n.tone as Tone)) return "gauge.tone invalid";
  if (n.label !== undefined) {
    if (typeof n.label !== "string") return "gauge.label must be string";
    if (n.label.length > MAX_LABEL_CHARS) return "gauge.label exceeds 200 chars";
  }
  return null;
}

// One node's LEAF checks (children handled by the caller's traversal).
function checkLeaf(n: Record<string, unknown>): string | null {
  console.assert(typeof n === "object" && n !== null, "checkLeaf: object required");
  console.assert(typeof n.type === "string", "checkLeaf: type must be string");
  // The `when` key is a bind-time construct (§5 conditions): the server evaluates it
  // and strips it before writing the bound snapshot. A surviving `when` on any node
  // therefore means the payload is not a bound scene — refuse it outright.
  if ("when" in n) return "when key must be stripped server-side";
  const t = n.type as string;
  if (RESERVED_TYPES.has(t)) return `reserved type refused: ${t}`;
  if (t === "repeat") return "repeat must be expanded server-side before rendering";
  if (t === "divider") return null;
  if (t === "text") return checkText(n);
  if (t === "number") return checkNumber(n);
  if (t === "chip") return checkChip(n);
  if (t === "bar") return checkBar(n);
  if (t === "icon") return checkIcon(n);
  if (t === "spark") return checkSpark(n);
  if (t === "gauge") return checkGauge(n);
  if (t === "stack") {
    if (!STACK_DIRS.includes(n.dir as StackDir)) return "stack.dir invalid";
    if (!STACK_GAPS.includes(n.gap as StackGap)) return "stack.gap invalid";
    if (!Array.isArray(n.children)) return "stack.children must be array";
    return null;
  }
  if (t === "grid") {
    const c = n.cols;
    if (typeof c !== "number" || c < 2 || c > 4 || !Number.isInteger(c)) return "grid.cols must be 2..4";
    if (!Array.isArray(n.children)) return "grid.children must be array";
    return null;
  }
  return `unknown node type: ${t}`;
}

/** Validate a BOUND scene. Returns null when ok, else a short reason.
 *  Iterative traversal — no recursion (NASA §1); an explicit stack tracks depth
 *  and the total-node budget together so a deep or wide tree fails fast. */
export function validateBoundScene(root: unknown): string | null {
  console.assert(root !== undefined, "validateBoundScene: root defined");
  console.assert(arguments.length === 1, "validateBoundScene: one arg");
  if (!isRecord(root)) return "scene must be an object";
  const stack: { node: Record<string, unknown>; depth: number }[] = [{ node: root, depth: 1 }];
  let count = 0;
  while (stack.length > 0) {
    const frame = stack.pop();
    if (!frame) break;
    const { node, depth } = frame;
    count += 1;
    if (count > MAX_NODES) return "scene exceeds 100 nodes";
    if (depth > MAX_DEPTH) return "scene exceeds depth 8";
    if (!isRecord(node)) return "child must be an object";
    const err = checkLeaf(node);
    if (err) return err;
    const children = (node.type === "stack" || node.type === "grid") ? node.children : null;
    if (!Array.isArray(children)) continue;
    for (const child of children) {
      if (!isRecord(child)) return "child must be an object";
      stack.push({ node: child, depth: depth + 1 });
    }
  }
  return null;
}

// Cached formatters — Intl.NumberFormat construction is not free and every card renders.
const compactFmt = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const percentFmt = new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1 });
const currencyFmt = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" });
const plainFmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

/** Format a number for the scene's `number` node. `unit` is appended to plain/compact
 *  only — percent/currency carry their own suffix. Pure; safe for any locale. */
export function formatNumber(value: number, format: NumberFormat, unit?: string): string {
  console.assert(typeof value === "number", "formatNumber: value must be number");
  console.assert(NUMBER_FORMATS.includes(format), "formatNumber: format in enum");
  if (!Number.isFinite(value)) return "—";
  if (format === "percent") return percentFmt.format(value);
  if (format === "currency") return currencyFmt.format(value);
  const base = format === "compact" ? compactFmt.format(value) : plainFmt.format(value);
  const u = typeof unit === "string" ? unit.trim() : "";
  return u ? `${base} ${u}` : base;
}
