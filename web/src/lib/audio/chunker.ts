// Sentence chunking for spoken streaming: speak sentences AS the reply streams instead
// of after it finishes — the difference between "listening to a conversation" and
// "waiting, then being read an essay". Markdown noise (headers, emphasis, code fences,
// link syntax) is stripped so system voices don't read asterisks aloud.

/** Strip the markdown that reads badly aloud; keep the words. */
export function speakableText(md: string): string {
  return md
    .replace(/```[\s\S]*?(```|$)/g, " code block omitted. ") // fenced code: never read aloud
    .replace(/`([^`]*)`/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/[*_~]{1,3}([^*_~]+)[*_~]{1,3}/g, "$1")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1") // links/images -> their text
    .replace(/^\s*[-+*]\s+/gm, "") // bullet markers
    .replace(/\|/g, " ") // table pipes
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Split the COMPLETE sentences off the front of `buffer`; return them plus the
 * unfinished remainder to keep buffering. A sentence ends at . ! ? or a newline,
 * but never inside a decimal like "0.9" (digit on both sides of the dot).
 */
export function sentenceChunks(buffer: string): { chunks: string[]; rest: string } {
  const chunks: string[] = [];
  let rest = buffer;
  for (;;) {
    let cut = -1;
    for (let i = 0; i < rest.length; i++) {
      const c = rest[i];
      if (c === "\n") { cut = i; break; }
      if (c === "." || c === "!" || c === "?") {
        const prev = rest[i - 1] ?? "";
        const next = rest[i + 1] ?? "";
        if (c === "." && /\d/.test(prev) && /\d/.test(next)) continue; // 0.9, 3.14
        if (next && !/\s/.test(next)) continue; // e.g., v0.9.14 or file.txt mid-token
        cut = i;
        break;
      }
    }
    if (cut === -1) break;
    const sentence = rest.slice(0, cut + 1).trim();
    rest = rest.slice(cut + 1);
    if (sentence) chunks.push(sentence);
  }
  return { chunks, rest };
}
