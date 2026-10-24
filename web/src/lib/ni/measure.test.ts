import { describe, expect, it } from "vitest";

import type { NiMeasure, NiMeasureCounts } from "$lib/api";
import { measureRows } from "./measure";

const zero: NiMeasureCounts = { kept: 0, dismissed: 0, waiting: 0, live: 0, failing: 0, paused: 0, gone: 0 };

function measure(over: Partial<NiMeasure["by_source"]>): NiMeasure {
  const by = { library: zero, own: zero, research: zero, paste: zero, other: zero, ...over };
  return { since: "2026-10-24T12:00:00+00:00", by_source: by, total: zero };
}

describe("measureRows", () => {
  it("says how many were kept and how the kept ones stand a week on", () => {
    const rows = measureRows(measure({
      library: { ...zero, kept: 3, dismissed: 1, live: 2, gone: 1 },
      research: { ...zero, kept: 1, waiting: 1 },
      paste: { ...zero, dismissed: 2 },
    }));
    expect(rows.map((r) => r.source)).toEqual(["library", "research", "paste"]);
    expect(rows[0].line).toBe("3 kept, 1 said no · 2 of 3 still working after 7 days");
    expect(rows[1].line).toBe("1 kept, 0 said no · 7-day check pending");
    expect(rows[2].line).toBe("0 kept, 2 said no");
  });

  it("shows nothing before any verdict", () => {
    expect(measureRows(measure({}))).toEqual([]);
    expect(measureRows(null)).toEqual([]);
  });
});
