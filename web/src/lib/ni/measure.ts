// Release P4: the live measure in plain words, one line per way a card was built (the Status page renders these).
import type { NiMeasure, NiMeasureSource } from "$lib/api";

const LABELS: Record<NiMeasureSource, string> = {
  library: "From the SmartBrain Library",
  own: "From a source you kept",
  research: "Found by research",
  paste: "From a link you pasted",
  other: "Other cards",
};
const ORDER: NiMeasureSource[] = ["library", "own", "research", "paste", "other"];

export interface MeasureRow {
  source: NiMeasureSource;
  label: string;
  line: string;
}

/** One row per way in that has any verdict: "3 kept, 1 said no · 2 of 3 still working after 7 days". */
export function measureRows(m: NiMeasure | null): MeasureRow[] {
  if (!m) return [];
  const out: MeasureRow[] = [];
  for (const source of ORDER) {
    const c = m.by_source[source];
    if (!c || c.kept + c.dismissed === 0) continue;
    const checked = c.live + c.failing + c.paused + c.gone;
    const verdicts = `${c.kept} kept, ${c.dismissed} said no`;
    const week = checked > 0
      ? `${c.live} of ${checked} still working after 7 days`
      : c.kept > 0 ? "7-day check pending" : "";
    out.push({ source, label: LABELS[source], line: week ? `${verdicts} · ${week}` : verdicts });
  }
  return out;
}
