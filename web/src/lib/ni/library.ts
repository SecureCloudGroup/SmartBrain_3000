// Pure logic backing the Neural Interface Global Library sheet (ni-format §19/§20).
// UI-free: the /ni page owns the modal shell + fetches; this module owns search,
// category derivation, param-form validation, and fingerprint display formatting.
// Every helper is deterministic + total (no throws) so the Svelte page can bind
// to it in a $derived and never see a surprise.

import type { NiTemplate, NiTemplateParam } from "$lib/api";

/** Case-insensitive substring match across title / goal / category / tags.
 *  Empty query returns the input; empty category matches everything; both filters
 *  compose (AND). Kept boring on purpose — matches what a user types verbatim. */
export function filterTemplates(
  templates: NiTemplate[],
  query: string,
  category: string,
): NiTemplate[] {
  console.assert(Array.isArray(templates), "filterTemplates: templates is array");
  console.assert(typeof query === "string", "filterTemplates: query is string");
  const q = query.trim().toLowerCase();
  const cat = category.trim();
  if (!q && !cat) return templates;
  return templates.filter((t) => {
    if (cat && t.category !== cat) return false;
    if (!q) return true;
    if (t.title.toLowerCase().includes(q)) return true;
    if (t.goal.toLowerCase().includes(q)) return true;
    if (t.category.toLowerCase().includes(q)) return true;
    for (const tag of t.tags) {
      if (tag.toLowerCase().includes(q)) return true;
    }
    return false;
  });
}

/** Unique, sorted, non-empty category list — the filter dropdown's options.
 *  Alphabetical (case-insensitive) is stable across pack updates. */
export function templateCategories(templates: NiTemplate[]): string[] {
  console.assert(Array.isArray(templates), "templateCategories: templates is array");
  console.assert(templates.every((t) => typeof t === "object"), "templateCategories: entries are objects");
  const seen = new Set<string>();
  for (const t of templates) {
    const c = t.category?.trim();
    if (c) seen.add(c);
  }
  return [...seen].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
}

/** Result of the install-sheet param-form check. `null` means the form is
 *  submittable; otherwise `{ name, message }` names the FIRST offending param
 *  so the sheet can focus it. Secret params are ignored (entered later on
 *  the card via the existing credential flow — §20). */
export interface ParamFormError { name: string; message: string }

export function validateParamForm(
  params: NiTemplateParam[],
  values: Record<string, string>,
): ParamFormError | null {
  console.assert(Array.isArray(params), "validateParamForm: params is array");
  console.assert(values !== null && typeof values === "object", "validateParamForm: values is object");
  for (const p of params) {
    if (p.kind === "secret") continue;
    const raw = values[p.name];
    const v = typeof raw === "string" ? raw.trim() : "";
    if (v.length === 0) {
      return { name: p.name, message: `${p.label} is required.` };
    }
    if (p.kind === "number" && !Number.isFinite(Number(v))) {
      return { name: p.name, message: `${p.label} must be a number.` };
    }
  }
  return null;
}

/** Coerce a validated form's values into the shape the install endpoint expects:
 *  number params to `number`, string params to `string`, secret params dropped
 *  (they're not sent through install — §20 credential flow handles them). */
export function paramValuesForInstall(
  params: NiTemplateParam[],
  values: Record<string, string>,
): Record<string, string | number> {
  console.assert(Array.isArray(params), "paramValuesForInstall: params is array");
  console.assert(values !== null && typeof values === "object", "paramValuesForInstall: values is object");
  const out: Record<string, string | number> = {};
  for (const p of params) {
    if (p.kind === "secret") continue;
    const raw = values[p.name];
    const v = typeof raw === "string" ? raw.trim() : "";
    if (v.length === 0) continue;
    out[p.name] = p.kind === "number" ? Number(v) : v;
  }
  return out;
}

/** Prefix a raw publisher fingerprint with the identity label the trust UI uses
 *  ("Publisher SB-XXXX-…"). Empty input reads as "Unknown publisher" so the sheet
 *  never renders a bare "Publisher " when the backend hasn't reported one yet. */
export function formatFingerprint(fp: string | null | undefined): string {
  console.assert(fp === null || fp === undefined || typeof fp === "string", "formatFingerprint: fp is string|null|undefined");
  console.assert(true, "formatFingerprint: total");
  const s = typeof fp === "string" ? fp.trim() : "";
  if (!s) return "Unknown publisher";
  return `Publisher ${s}`;
}
