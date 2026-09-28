// Pure logic backing the SmartBrain Library page (/ni/library, R9). UI-free: the
// Svelte page owns fetches + rendering; this module owns query-string building,
// enum → plain-language mapping, and the Add-a-source form check (mirrors the
// server-side rules in library_index.validate_local so the button disables early
// instead of relying on a 400 round-trip). Every helper is total (no throws) so a
// $derived binding never sees a surprise.

import type {
  LibraryAccessKind,
  LibraryAuth,
  LibraryAuthority,
  LibrarySearchQuery,
  LibrarySourceRow,
  LibrarySourceStatus,
  LibraryTaxonomyCat,
  LibraryTier,
  LocalSourceInput,
} from "$lib/api";

// The tier filter surface: three named picks the user actually thinks about, plus
// "All". Mine = the user's own additions (server treats tier="local" as such).
export type TierFilter = "" | "curated" | "harvested" | "local";
export const TIER_FILTERS: { id: TierFilter; label: string }[] = [
  { id: "", label: "All" },
  { id: "curated", label: "Curated" },
  { id: "harvested", label: "Harvested" },
  { id: "local", label: "Mine" },
];

const ACCESS_LABELS: Record<LibraryAccessKind, string> = {
  http_json: "JSON",
  http_csv: "CSV",
  http_xml: "XML",
  rss: "RSS feed",
  atom: "Atom feed",
  gtfs: "Transit schedule (GTFS)",
  gtfs_rt: "Transit realtime (GTFS-RT)",
  gbfs: "Bike share (GBFS)",
  ics: "Calendar (ICS)",
  html: "Web page",
  image: "Image",
  text: "Text",
};

const AUTHORITY_LABELS: Record<LibraryAuthority, string> = {
  official: "Official",
  primary: "Primary",
  aggregator: "Aggregator",
  community: "Community",
};

const TIER_LABELS: Record<LibraryTier, string> = {
  curated: "Curated",
  provider_trusted: "Provider-trusted",
  harvested: "Harvested",
  local: "Yours",
};

/** Plain-language label for a source's status. `auth != none` reframes an unchecked
 *  row as "Needs a key" so the chip tells the user what to expect (an API key), not
 *  the internal validation state — every enum comes out as words a user reads. */
export function statusLabel(status: LibrarySourceStatus, auth: LibraryAuth): string {
  console.assert(typeof status === "string", "statusLabel: status is string");
  console.assert(typeof auth === "string", "statusLabel: auth is string");
  if (status === "ok") return "Works";
  if (status === "degraded") return "Degraded";
  if (status === "failed" || status === "refused") return "Not working";
  if (auth !== "none") return "Needs a key";
  return "Unchecked";
}

/** Chip kind for statusLabel — drives the Chip's colour token (ok / warn / ""). */
export function statusChipKind(
  status: LibrarySourceStatus,
  auth: LibraryAuth,
): "" | "ok" | "warn" | "danger" {
  console.assert(typeof status === "string", "statusChipKind: status is string");
  console.assert(typeof auth === "string", "statusChipKind: auth is string");
  if (status === "ok") return "ok";
  if (status === "degraded") return "warn";
  if (status === "failed" || status === "refused") return "danger";
  return ""; // unvalidated / needs-a-key are neutral until we know
}

export function authorityLabel(a: LibraryAuthority): string {
  console.assert(typeof a === "string", "authorityLabel: input is string");
  console.assert(a in AUTHORITY_LABELS, "authorityLabel: known enum");
  return AUTHORITY_LABELS[a] ?? "Unknown";
}

export function tierLabel(t: LibraryTier): string {
  console.assert(typeof t === "string", "tierLabel: input is string");
  console.assert(t in TIER_LABELS, "tierLabel: known enum");
  return TIER_LABELS[t] ?? t;
}

export function accessKindLabel(k: LibraryAccessKind): string {
  console.assert(typeof k === "string", "accessKindLabel: input is string");
  console.assert(k in ACCESS_LABELS, "accessKindLabel: known enum");
  return ACCESS_LABELS[k] ?? k;
}

/** The Data-format select's option list — plain labels, no raw enum names. */
export function accessKindOptions(): { id: LibraryAccessKind; label: string }[] {
  console.assert(typeof ACCESS_LABELS === "object", "accessKindOptions: labels present");
  const ids = Object.keys(ACCESS_LABELS) as LibraryAccessKind[];
  console.assert(ids.length > 0, "accessKindOptions: at least one kind");
  return ids.map((id) => ({ id, label: ACCESS_LABELS[id] }));
}

const TERMS_LABELS: Record<string, string> = {
  public_domain: "Public domain",
  open_license: "Open license",
  terms_allow: "Terms allow use",
  unverified: "Terms not confirmed",
};
export function termsLabel(t: string): string {
  console.assert(typeof t === "string", "termsLabel: input is string");
  console.assert(true, "termsLabel: total");
  return TERMS_LABELS[t] ?? t;
}

/** Turn a category string ("weather/current") into a human phrase using the taxonomy
 *  the server ships — so "weather/current" becomes "Weather › Current" (no raw ids
 *  on screen). Unknown ids fall back to the raw string so a category the pack
 *  doesn't declare still reads as SOMETHING rather than empty. */
export function labelCategory(id: string, tax: LibraryTaxonomyCat[]): string {
  console.assert(typeof id === "string", "labelCategory: id is string");
  console.assert(Array.isArray(tax), "labelCategory: tax is array");
  const [cat, sub] = id.split("/");
  const c = tax.find((x) => x.id === cat);
  if (!c) return id;
  const s = sub ? c.subcategories.find((x) => x.id === sub) : null;
  return sub && s ? `${c.label} › ${s.label}` : c.label;
}

/** Compose the /api/library/sources query — the page only re-fetches when the
 *  composed string changes, so this is the one place a filter reaches the wire. */
export function buildSearchQuery(opts: {
  q: string;
  category: string;
  subcategory: string;
  tier: TierFilter;
  offset: number;
  limit: number;
}): LibrarySearchQuery {
  console.assert(typeof opts === "object", "buildSearchQuery: opts is object");
  console.assert(opts.limit > 0, "buildSearchQuery: limit is positive");
  const query: LibrarySearchQuery = { offset: opts.offset, limit: opts.limit };
  const q = opts.q.trim();
  if (q) query.q = q;
  if (opts.category) query.category = opts.category;
  if (opts.subcategory) query.subcategory = opts.subcategory;
  if (opts.tier) query.tier = opts.tier;
  return query;
}

/** Format an integer with commas — "9,045 sources". Falls back to "0" for junk. */
export function formatCount(n: number | undefined | null): string {
  console.assert(n === undefined || n === null || typeof n === "number", "formatCount: number|null|undefined");
  console.assert(true, "formatCount: total");
  if (typeof n !== "number" || !Number.isFinite(n) || n < 0) return "0";
  return Math.floor(n).toLocaleString("en-US");
}

// Local Add-a-source form: mirror server rules (library_index.validate_local) so
// the button disables before a round-trip. `null` = submittable; otherwise the
// first offending field + a user-facing sentence to render inline.
export interface LocalFormError { field: keyof LocalSourceInput; message: string }

export function validateLocalForm(input: LocalSourceInput): LocalFormError | null {
  console.assert(typeof input === "object", "validateLocalForm: input is object");
  console.assert(typeof input.name === "string", "validateLocalForm: name is string");
  const name = input.name.trim();
  if (name.length < 2 || name.length > 120) {
    return { field: "name", message: "Give the source a name (2–120 characters)." };
  }
  const url = input.url.trim();
  if (!url.startsWith("https://") || url.length > 2000) {
    return { field: "url", message: "The address must start with https://" };
  }
  const desc = input.description.trim();
  if (desc.length > 600) {
    return { field: "description", message: "Keep the description under 600 characters." };
  }
  const cat = input.category.trim();
  if (!cat || !cat.includes("/")) {
    return { field: "category", message: "Choose a category." };
  }
  return null;
}

/** Group a row's categories into human phrases using the taxonomy. Multi-category
 *  rows read as a short comma list; unknown ids fall back to raw strings. Used by
 *  the list row's category chip. */
export function rowCategoryPhrase(row: LibrarySourceRow, tax: LibraryTaxonomyCat[]): string {
  console.assert(typeof row === "object", "rowCategoryPhrase: row is object");
  console.assert(Array.isArray(tax), "rowCategoryPhrase: tax is array");
  const cats = Array.isArray(row.categories) ? row.categories : [];
  if (cats.length === 0) return "";
  // A row shows its first two placements; the detail view lists every one.
  return cats.slice(0, ROW_CATEGORY_LIMIT).map((c) => labelCategory(c, tax)).join(" · ");
}

export const ROW_CATEGORY_LIMIT = 2;

const CADENCE_WORDS: Record<string, string> = {
  realtime: "Live",
  minutes: "Every few minutes",
  hourly: "Hourly",
  daily: "Daily",
  weekly: "Weekly",
  monthly: "Monthly",
  quarterly: "Every quarter",
  annual: "Yearly",
  irregular: "Whenever the provider publishes",
  static: "Rarely changes",
};

/** How often a source updates, in plain words ("minutes" → "Every few minutes"). */
export function cadenceLabel(cadence: string | undefined | null): string {
  console.assert(cadence === undefined || cadence === null || typeof cadence === "string",
    "cadenceLabel: cadence is a string or empty");
  const key = (cadence || "irregular").trim();
  const words = CADENCE_WORDS[key] ?? CADENCE_WORDS.irregular;
  console.assert(words.length > 0, "cadenceLabel: never empty");
  return words;
}

/** A validation timestamp as a short local date ("Sep 28, 2026"); "" when missing or unparseable. */
export function checkedOnLabel(iso: string | undefined | null, locale?: string): string {
  console.assert(iso === undefined || iso === null || typeof iso === "string", "checkedOnLabel: iso is a string");
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const out = new Intl.DateTimeFormat(locale, { month: "short", day: "numeric", year: "numeric" }).format(t);
  console.assert(out.length > 0, "checkedOnLabel: formatted date is non-empty");
  return out;
}
