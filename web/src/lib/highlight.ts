// Split text into plain/matched segments so search hits can be visibly marked.
//
// Returns SEGMENTS rather than an HTML string on purpose: the caller renders them with Svelte's
// normal text interpolation, so document content is never injected as markup. Highlighting a
// document must not become a way to execute it.

export interface Segment {
  t: string;
  hit: boolean;
}

const ESCAPE = /[.*+?^${}()|[\]\\]/g;

/** Case-insensitive occurrences of any `term` in `text`, as alternating plain/hit segments. */
export function highlight(text: string, terms: string[]): Segment[] {
  const clean = terms.map((t) => t.trim()).filter(Boolean);
  if (!text || clean.length === 0) return [{ t: text, hit: false }];
  const pattern = clean
    .sort((a, b) => b.length - a.length) // longest first, so "lease" wins over "lea"
    .map((t) => t.replace(ESCAPE, "\\$&"))
    .join("|");
  const re = new RegExp(`(${pattern})`, "gi");
  const out: Segment[] = [];
  let last = 0;
  for (const m of text.matchAll(re)) {
    const i = m.index ?? 0;
    if (i > last) out.push({ t: text.slice(last, i), hit: false });
    out.push({ t: m[0], hit: true });
    last = i + m[0].length;
  }
  if (last < text.length) out.push({ t: text.slice(last), hit: false });
  return out;
}

/** The words of a query, as search terms. Mirrors the backend tokenizer closely enough to mark them. */
export function queryTerms(query: string): string[] {
  return query.toLowerCase().match(/[^\W_]+/gu) ?? [];
}
