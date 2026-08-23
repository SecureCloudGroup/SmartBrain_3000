import { describe, expect, it } from "vitest";
import { sentenceChunks, speakableText } from "./chunker";

describe("sentenceChunks", () => {
  it("splits complete sentences and keeps the unfinished tail", () => {
    const { chunks, rest } = sentenceChunks("First one. Second one! And then");
    expect(chunks).toEqual(["First one.", "Second one!"]);
    expect(rest).toBe(" And then");
  });
  it("does not split inside decimals or version numbers", () => {
    const { chunks, rest } = sentenceChunks("We shipped v0.9.14 today. More soon");
    expect(chunks).toEqual(["We shipped v0.9.14 today."]);
    expect(rest).toBe(" More soon");
  });
  it("treats newlines as sentence ends", () => {
    const { chunks } = sentenceChunks("A line without a period\nNext line.");
    expect(chunks[0]).toBe("A line without a period");
  });
  it("returns everything as rest when nothing is complete", () => {
    const { chunks, rest } = sentenceChunks("still streaming");
    expect(chunks).toEqual([]);
    expect(rest).toBe("still streaming");
  });
});

describe("speakableText", () => {
  it("strips markdown that reads badly aloud", () => {
    expect(speakableText("## Header\n**bold** and `code` and [a link](https://x.example)"))
      .toBe("Header bold and code and a link");
  });
  it("replaces fenced code with a spoken placeholder", () => {
    expect(speakableText("Before\n```js\nconst x = 1;\n```\nAfter")).toContain("code block omitted");
  });
});
