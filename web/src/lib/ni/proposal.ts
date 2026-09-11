// L2 frontier-repair proposal helpers (ni-format §23) — pure logic backing the
// "Fix proposed — review" surface on /ni. UI-free: the review modal owns the
// fetch + Apply/Dismiss flow; this module owns extracting the CURRENT stages
// out of an item's spec so the diff view can render them next to the proposed
// ones. Deterministic + total (never throws) so the Svelte page can bind it in
// a $derived without surprises.

/** The two stage shapes the diff view cares about (§4.1/§4.2). Everything else
 *  in a pipeline (llm, other future ops) is deliberately IGNORED — the L2
 *  repair surface only ever proposes extract/transform (§23 closed schema). */
export interface StageSlice {
  extract: Record<string, unknown> | null;
  transform: unknown[] | null;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Pull the extract paths map + the transform apply-list out of an item spec.
 *  The spec's inner shape is unstructured (Record<string, unknown> in api.ts),
 *  so we defensively pattern-match the two stages we care about and return
 *  `null` for either that isn't present or is malformed. Any unexpected shape
 *  becomes `null` — the diff view then just says "(none)" for that side. */
export function stagesFromSpec(spec: Record<string, unknown> | null | undefined): StageSlice {
  console.assert(spec === null || spec === undefined || typeof spec === "object", "stagesFromSpec: spec is object|null|undefined");
  console.assert(!Array.isArray(spec), "stagesFromSpec: spec is not an array");
  const out: StageSlice = { extract: null, transform: null };
  if (!isRecord(spec)) return out;
  const pipeline = spec.pipeline;
  if (!Array.isArray(pipeline)) return out;
  for (const stage of pipeline) {
    if (!isRecord(stage)) continue;
    if (stage.op === "extract" && isRecord(stage.paths)) {
      // First extract wins — an item spec has at most one extract stage in v1.
      if (out.extract === null) out.extract = stage.paths;
    } else if (stage.op === "transform" && Array.isArray(stage.apply)) {
      if (out.transform === null) out.transform = stage.apply;
    }
  }
  return out;
}

/** Pretty-print a stage slice's field for the diff <pre>. `null` renders as
 *  "(none)" so the modal never paints a bare "null" (the operator can't
 *  tell null-the-JSON from missing-the-stage at a glance). Uses 2-space
 *  indent — matches how the spec looks in the internal docs. */
export function formatStageJson(value: unknown): string {
  console.assert(value === null || typeof value === "object" || typeof value === "undefined", "formatStageJson: value is object|null|undefined");
  console.assert(true, "formatStageJson: total");
  if (value === null || value === undefined) return "(none)";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    // A cyclic or otherwise non-serializable value never comes from the server
    // (JSON in over the wire), but the try/catch keeps the helper total.
    return "(unserializable)";
  }
}
