// Comma-string <-> tag-list helpers shared by every tag editor (Planner tasks,
// Knowledge documents, Vaults). One place, one behavior: trim, drop blanks.
// The server cleans again (de-dupe + bound), so these stay deliberately minimal.

export function strToTags(s: string): string[] {
  return s.split(",").map((t) => t.trim()).filter(Boolean);
}

export function tagsToStr(tags: string[] | undefined): string {
  return (tags ?? []).join(", ");
}
