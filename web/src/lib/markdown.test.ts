// @vitest-environment jsdom
//
// The chat used to print the model's raw markdown (literal `###` and `**`). renderMarkdown fixes
// that — but it turns UNTRUSTED model output into HTML, so the sanitizer is the load-bearing part:
// a reply can quote an attacker-controlled web page or PDF verbatim, and injected script would run
// in the app origin with an unlocked session. These tests pin both halves: it renders, and it scrubs.

import { describe, expect, it } from "vitest";

import { renderMarkdown } from "./markdown";

describe("renderMarkdown — rendering", () => {
  it("renders headings, bold and lists instead of raw markdown characters", () => {
    const html = renderMarkdown("### Key Details\n\n**Scope:** the SPAC IPO\n\n- one\n- two");
    expect(html).toContain("<h3");
    expect(html).toContain("<strong>Scope:</strong>");
    expect(html).toContain("<li>one</li>");
    expect(html).not.toContain("###"); // the bug this fixes
    expect(html).not.toContain("**");
  });

  it("renders fenced code blocks (the copy button anchors to <pre>)", () => {
    const html = renderMarkdown("```python\nprint('hi')\n```");
    expect(html).toContain("<pre>");
    expect(html).toContain("<code");
    expect(html).toContain("print(");
  });

  it("renders GFM tables", () => {
    const html = renderMarkdown("| a | b |\n| - | - |\n| 1 | 2 |");
    expect(html).toContain("<table>");
    expect(html).toContain("<td>1</td>");
  });

  it("survives half-finished markdown (a reply still streaming)", () => {
    expect(() => renderMarkdown("### Partly written\n\n```py\nprint(")).not.toThrow();
    expect(renderMarkdown("")).toBe("");
  });
});

describe("renderMarkdown — sanitizing untrusted model output", () => {
  it("strips <script> smuggled through a quoted document", () => {
    const html = renderMarkdown("Summary: <script>alert('pwned')</script> done");
    expect(html).not.toContain("<script");
    expect(html).not.toContain("alert(");
  });

  it("strips event-handler attributes (e.g. an <img onerror> payload)", () => {
    const html = renderMarkdown('<img src="x" onerror="fetch(\'/api/secrets\')">');
    expect(html).not.toContain("onerror");
    expect(html).not.toContain("fetch(");
  });

  it("neutralizes javascript: links", () => {
    const html = renderMarkdown("[click me](javascript:alert(1))");
    expect(html).not.toContain("javascript:");
  });

  it("drops iframes and inline styles", () => {
    const html = renderMarkdown('<iframe src="https://evil.test"></iframe><p style="position:fixed">x</p>');
    expect(html).not.toContain("<iframe");
    expect(html).not.toContain("style=");
  });

  it("opens real links in a new tab without an opener reference", () => {
    const html = renderMarkdown("[docs](https://example.com)");
    expect(html).toContain('target="_blank"');
    expect(html).toContain("noopener");
  });
});
