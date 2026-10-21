// Per-card text inputs for the NI board: one record per field, keyed by item id, so two
// cards waiting on the same kind of input never share a value.
export type CardText = Record<string, string>;

export function cardText(rec: CardText, id: string): string {
  console.assert(typeof id === "string" && id.length > 0, "cardText: id required");
  console.assert(typeof rec === "object" && rec !== null, "cardText: record required");
  return rec[id] ?? "";
}

// A copy of `rec` with only `id`'s entry removed (clears one card, leaves the rest).
export function withoutCardText(rec: CardText, id: string): CardText {
  console.assert(typeof id === "string" && id.length > 0, "withoutCardText: id required");
  console.assert(typeof rec === "object" && rec !== null, "withoutCardText: record required");
  const { [id]: _dropped, ...rest } = rec;
  return rest;
}
