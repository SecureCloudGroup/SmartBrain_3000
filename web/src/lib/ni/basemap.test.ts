// basemap.ts — the coastline projection IrPaint paints for basemap prims, pinned against the
// basemap branch of the Python painter (app/smartbrain_3000/ni_forms/paint/vector.py,
// `_svg_prim`, k == "basemap"). Every Python literal below was computed with that branch's
// formula and `_f` over the shipped asset. Node environment.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it, vi } from "vitest";

import { mapPaths, parseWorld, type Ring, type Rings } from "./basemap";
import { validateClir, type BasemapPrim } from "./clir";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = join(HERE, "..", "..", "..", "..", "app");
const WORLD = join(APP, "smartbrain_3000", "ni_forms", "assets", "world110m.json");
const FIXTURE = join(APP, "tests", "fixtures", "ni_forms", "clir", "every_kind.clir.json");

function readJson(path: string): unknown {
  return JSON.parse(readFileSync(path, "utf8"));
}
const world: Rings = parseWorld(readJson(WORLD));
/** "Mx,y Lx,y …Z" → its first three "x,y" tokens. */
const first3 = (d: string) => d.slice(1, -1).split(" L").slice(0, 3);

/** A prim whose box is 100 × 50 px at (10, 20) and whose bbox is lon −10…10, lat 40…50,
 *  so x = 10 + (lon + 10) · 5 and y = 20 + (50 − lat) · 5. */
function prim(over: Partial<BasemapPrim> = {}): BasemapPrim {
  return {
    k: "basemap", id: 7, asset: "world110m",
    box: { x0: ["l", 10], x1: ["l", 110], y0: 20, y1: 70 },
    bbox: [-10, 40, 10, 50], land: "map-land", stroke: "map-stroke",
    ...over,
  };
}

describe("mapPaths", () => {
  it("projects a ring into the box with the Python mapping and number format", () => {
    // (−10, 50) → (10, 20); (2.5, 47.25) → (10 + 12.5 · 5, 20 + 2.75 · 5) = (72.5, 33.75);
    // (10, 40) → (110, 70). Closed with Z, as vector.py writes it.
    const ring: Ring = [[-10, 50], [2.5, 47.25], [10, 40], [-10, 50]];
    expect(mapPaths(prim(), 300, [ring])).toEqual(["M10,20 L72.5,33.75 L110,70 L10,20Z"]);
  });

  it("resolves the box's x anchors against the paint width", () => {
    const p = prim({ box: { x0: ["l", 0], x1: ["f", 0.5], y0: 20, y1: 70 } });
    const ring: Ring = [[-10, 50], [10, 40], [-10, 50]];
    expect(mapPaths(p, 200, [ring])).toEqual(["M0,20 L100,70 L0,20Z"]);
    expect(mapPaths(p, 400, [ring])).toEqual(["M0,20 L200,70 L0,20Z"]);
  });

  it("skips a ring whose whole extent lies outside the bbox", () => {
    const east: Ring = [[20, 40], [30, 40], [30, 50], [20, 40]];
    const north: Ring = [[0, 60], [5, 60], [5, 70], [0, 60]];
    const west: Ring = [[-30, 45], [-20, 45], [-20, 48], [-30, 45]];
    const south: Ring = [[0, 30], [5, 30], [5, 35], [0, 30]];
    expect(mapPaths(prim(), 300, [east, north, west, south])).toEqual([]);
  });

  it("keeps a ring straddling the bbox edge (the SVG clip trims it) and one touching it", () => {
    const straddling: Ring = [[5, 45], [15, 45], [15, 48], [5, 45]];
    expect(mapPaths(prim(), 300, [straddling])).toEqual(["M85,45 L135,45 L135,30 L85,45Z"]);
    // max(lon) == lon0: vector.py's strict `<` keeps it.
    const touching: Ring = [[-20, 45], [-10, 45], [-10, 48], [-20, 45]];
    expect(mapPaths(prim(), 300, [touching])).toEqual(["M-40,45 L10,45 L10,30 L-40,45Z"]);
  });

  it("gives a degenerate bbox (the validator's fallback) no projection", () => {
    expect(mapPaths(prim({ bbox: [0, 0, 0, 0] }), 300, world)).toEqual([]);
  });
});

describe("the shipped asset", () => {
  it("parses into the 125 closed rings vector.py iterates", () => {
    expect(world).toHaveLength(125);
    expect(world.reduce((n, r) => n + r.length, 0)).toBe(4575);
    for (const r of world) expect(r[0]).toEqual(r[r.length - 1]);
  });

  it("rejects a malformed payload as a whole", () => {
    expect(parseWorld(null)).toEqual([]);
    expect(parseWorld({ polys: "no" })).toEqual([]);
    expect(parseWorld({ polys: [[[0, 0], [1, 1], [0, 0]], [[2, "x"]]] })).toEqual([]);
    expect(parseWorld({ polys: [[[0, 0], [1, 1], [0, 0]]] })).toEqual([[[0, 0], [1, 1], [0, 0]]]);
  });
});

describe("every_kind fixture", () => {
  // Python, same prim at W = 424 (x1 = f0.48 → 203.52): the rings its skip rule keeps and
  // their point total. A byte-for-byte pin of the whole d is not possible: 17 of its 2204
  // y values are exact binary ties such as 187.125, which `_f` rounds half-to-even and
  // `toFixed` rounds up, so 13 tokens print 0.01 px apart (a difference `fmt` shares with
  // every other prim kind); everything else is identical, token for token.
  const KEPT = [51, 52, 53, 60, 61, 63, 64, 73, 74, 75, 90, 94];
  const clir = validateClir(readJson(FIXTURE));
  const bm = clir.prims.find((p): p is BasemapPrim => p.k === "basemap");

  it("keeps the rings the Python skip rule keeps and projects every point, at both bucket ends", () => {
    expect(bm).toBeDefined();
    if (!bm) return;
    expect(bm.bbox).toEqual([-130, 15, -60, 55]);
    expect([clir.bucket.min_w, clir.bucket.max_w]).toEqual([424, 496]);
    for (const width of [424, 496]) {
      const ds = mapPaths(bm, width, world);
      expect(ds).toHaveLength(KEPT.length);
      expect(world.flatMap((r, i) => (mapPaths(bm, width, [r]).length ? [i] : []))).toEqual(KEPT);
      expect(ds.join(" ").split(/[ML]/).length - 1).toBe(2204);
      for (const d of ds) expect(d).toMatch(/^M-?\d+(\.\d+)?,-?\d+(\.\d+)?( L-?\d+(\.\d+)?,-?\d+(\.\d+)?)+Z$/);
    }
  });

  it("writes the tie-free small rings byte for byte as the Python painter does", () => {
    if (!bm) return;
    const byRing = (i: number) => mapPaths(bm, 424, [world[i]])[0];
    expect(byRing(51)).toBe("M187.27,221.12 L186.51,221.81 L182.62,221.89 L182.47,220.73 L182.88,220.32 "
      + "L185.26,220.32 L186.74,220.57 L187.27,221.12Z");
    expect(byRing(61)).toBe("M152.52,205.91 L151.83,206.05 L151.1,204.45 L149.99,203.68 L150.63,201.92 "
      + "L151.51,202.03 L152.52,204.31 L152.52,205.91Z");
    expect(byRing(63)).toBe("M151.71,198.16 L148.54,198.59 L148.34,197.58 L149.7,197.36 L151.62,197.44 L151.71,198.16Z");
  });
});

describe("parity with vector.py on real rings", () => {
  // bbox [-130, 15, -60, 55]; box x0 = l0 → 0, x1 = f0.48 at width 424 → 203.52, y 120…230
  // (the every_kind prim). Python: x = 0 + (lon + 130) / 70 * 203.52, y = 120 + (55 − lat) / 40 * 110.
  const bm = prim({ box: { x0: ["l", 0], x1: ["f", 0.48], y0: 120, y1: 230 }, bbox: [-130, 15, -60, 55] });

  it("ring 51 (first point [-65.59, 18.23]) opens as Python's _f writes it", () => {
    expect(world[51][0]).toEqual([-65.59, 18.23]);
    expect(first3(mapPaths(bm, 424, [world[51]])[0]))
      .toEqual(["187.27,221.12", "186.51,221.81", "182.62,221.89"]);
  });

  it("ring 53 (first point [-72.58, 19.87]) opens as Python's _f writes it", () => {
    expect(world[53][0]).toEqual([-72.58, 19.87]);
    expect(first3(mapPaths(bm, 424, [world[53]])[0]))
      .toEqual(["166.94,216.61", "169.47,217.05", "169.82,216.58"]);
  });
});

describe("loadWorld", () => {
  const payload = JSON.stringify({ polys: [[[0, 0], [1, 1], [0, 0]]] });
  const ok = () => new Response(payload, { status: 200, headers: { "content-type": "application/json" } });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("fetches the same-origin asset once and hands every caller the same rings", async () => {
    const fetchMock = vi.fn(async () => ok());
    vi.stubGlobal("fetch", fetchMock);
    const mod = await import("./basemap");
    const a = await mod.loadWorld();
    const b = await mod.loadWorld();
    expect(a).toEqual([[[0, 0], [1, 1], [0, 0]]]);
    expect(b).toBe(a);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/ni/world110m.json");
  });

  it("resolves a failed fetch to [] and retries on the next call", async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError("network"))
      .mockResolvedValueOnce(new Response("", { status: 404 }))
      .mockResolvedValueOnce(ok());
    vi.stubGlobal("fetch", fetchMock);
    const mod = await import("./basemap");
    expect(await mod.loadWorld()).toEqual([]);
    expect(await mod.loadWorld()).toEqual([]);
    expect(await mod.loadWorld()).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
