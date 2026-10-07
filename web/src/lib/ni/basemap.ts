// basemap.ts — the coastline basemap IrPaint paints for `basemap` prims. The land rings are
// the asset the Python painter draws (app/smartbrain_3000/ni_forms/assets/world110m.json,
// copied to static/ni/ by scripts/gen-ni-tokens.mjs). `mapPaths` is a port of the basemap
// branch of app/smartbrain_3000/ni_forms/paint/vector.py (`_svg_prim`, k == "basemap"):
// the same equirectangular mapping into the prim's box, the same skip rule for rings that
// miss the bbox, the same number format. Only `loadWorld` touches the network.

import { resolveX, type BasemapPrim } from "./clir";

/** A closed ring of [lon, lat] points in degrees (first == last). */
export type Ring = Array<[number, number]>;
export type Rings = Ring[];

export const WORLD_URL = "/ni/world110m.json";

let pending: Promise<Rings> | null = null;

/** The land rings, fetched once per page. A failed fetch (network, status, shape) resolves
 *  to [] and is not cached, so the next call retries. */
export function loadWorld(): Promise<Rings> {
  console.assert(WORLD_URL.startsWith("/"), "loadWorld: same-origin asset");
  console.assert(typeof fetch === "function", "loadWorld: fetch available");
  if (pending) return pending;
  const p = fetchWorld().then((rings) => {
    if (rings.length === 0) pending = null;
    return rings;
  });
  pending = p;
  return p;
}

async function fetchWorld(): Promise<Rings> {
  console.assert(WORLD_URL.endsWith(".json"), "fetchWorld: json asset");
  try {
    const res = await fetch(WORLD_URL);
    console.assert(typeof res.ok === "boolean", "fetchWorld: a response");
    if (!res.ok) return [];
    return parseWorld(await res.json());
  } catch {
    return [];
  }
}

/** The asset's `polys` as rings; anything malformed rejects the whole asset ([]). */
export function parseWorld(raw: unknown): Rings {
  console.assert(raw !== undefined, "parseWorld: payload present");
  const polys = typeof raw === "object" && raw !== null && "polys" in raw ? raw.polys : null;
  if (!Array.isArray(polys)) return [];
  const list: unknown[] = polys;
  const out: Rings = [];
  for (const ring of list) {
    const pts = ringPoints(ring);
    if (pts === null) return [];
    out.push(pts);
  }
  console.assert(out.length === list.length, "parseWorld: every ring kept");
  return out;
}

/** One ring as [lon, lat] pairs, or null when malformed. */
function ringPoints(ring: unknown): Ring | null {
  console.assert(ring !== undefined, "ringPoints: ring present");
  if (!Array.isArray(ring) || ring.length === 0) return null;
  const pts: unknown[] = ring;
  const out: Ring = [];
  for (const q of pts) {
    if (!Array.isArray(q) || q.length !== 2) return null;
    const lon: unknown = q[0], lat: unknown = q[1];
    if (typeof lon !== "number" || typeof lat !== "number") return null;
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) return null;
    out.push([lon, lat]);
  }
  console.assert(out.length === pts.length, "ringPoints: every point kept");
  return out;
}

/** SVG path `d` for each land ring that touches the prim's bbox, projected into its box
 *  at `width`, as vector.py does: x = x0 + (lon − lon0) / (lon1 − lon0) · W2 and
 *  y = y0 + (lat1 − lat) / (lat1 − lat0) · H2 with W2, H2 the box size; a ring is skipped
 *  only when its whole extent lies outside the bbox. The clip to the box is the SVG's. */
export function mapPaths(prim: BasemapPrim, width: number, rings: Rings): string[] {
  console.assert(prim.k === "basemap", "mapPaths: basemap prim");
  console.assert(width >= 0, "mapPaths: width non-negative");
  const b = prim.box;
  const x0 = resolveX(b.x0, 0, width), x1 = resolveX(b.x1, 0, width);
  const [lon0, lat0, lon1, lat1] = prim.bbox;
  // The validator lets a malformed bbox through as [0, 0, 0, 0]; it has no projection.
  if (!(lon1 > lon0) || !(lat1 > lat0)) return [];
  const w2 = x1 - x0, h2 = b.y1 - b.y0;
  const out: string[] = [];
  for (const ring of rings) {
    if (missesBbox(ring, lon0, lat0, lon1, lat1)) continue;
    const pts = ring.map(([lo, la]) =>
      `${fmt(x0 + (lo - lon0) / (lon1 - lon0) * w2)},${fmt(b.y0 + (lat1 - la) / (lat1 - lat0) * h2)}`);
    out.push(`M${pts.join(" L")}Z`);
  }
  return out;
}

/** vector.py's skip rule: max(lon) < lon0 or min(lon) > lon1 or max(lat) < lat0 or min(lat) > lat1. */
function missesBbox(ring: Ring, lon0: number, lat0: number, lon1: number, lat1: number): boolean {
  console.assert(ring.length > 0, "missesBbox: non-empty ring");
  console.assert(lon1 > lon0 && lat1 > lat0, "missesBbox: ordered bbox");
  let minLon = Infinity, maxLon = -Infinity, minLat = Infinity, maxLat = -Infinity;
  for (const [lo, la] of ring) {
    if (lo < minLon) minLon = lo;
    if (lo > maxLon) maxLon = lo;
    if (la < minLat) minLat = la;
    if (la > maxLat) maxLat = la;
  }
  return maxLon < lon0 || minLon > lon1 || maxLat < lat0 || minLat > lat1;
}

/** vector.py's `_f` and IrPaint's `fmt`: two decimals, trailing zeros trimmed, no "-0". */
function fmt(v: number): string {
  console.assert(typeof v === "number", "fmt: number");
  console.assert(Number.isFinite(v), "fmt: finite number");
  const s = v.toFixed(2).replace(/\.?0+$/, "");
  return s === "-0" || s === "" ? "0" : s;
}
