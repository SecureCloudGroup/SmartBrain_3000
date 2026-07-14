// Render assistant markdown to safe HTML.
//
// Models emit markdown by default (headings, lists, **bold**, tables, code fences). Until now the
// chat rendered the raw string, so every reply showed literal `###` and `**` characters. We render it
// properly here.
//
// SANITIZE, ALWAYS. Assistant output is UNTRUSTED: the model can quote a fetched web page, an
// ingested PDF, or an email verbatim, so a crafted document could smuggle `<img onerror=...>` or a
// `javascript:` link into the reply. Injected script would run in the app's origin, which holds an
// unlocked session — so the model's HTML never reaches the DOM without passing through DOMPurify.

import DOMPurify from "dompurify";
import { marked } from "marked";

marked.setOptions({
  gfm: true, // tables, strikethrough, autolinks
  breaks: true, // a single newline is a <br> — matches how chat models format
});

// Links in a reply point off-site (and are attacker-influencable), so open them in a new tab and cut
// the opener reference. Registered once; DOMPurify hooks are global.
let hooked = false;
function installHooks(): void {
  if (hooked) return;
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (node.tagName === "A") {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer nofollow");
    }
  });
  hooked = true;
}

/** Markdown -> sanitized HTML. Safe to call on partial markdown while a reply is still streaming. */
export function renderMarkdown(src: string): string {
  if (typeof window === "undefined") return ""; // no DOM -> no sanitizer; never emit unsanitized HTML
  installHooks();
  const html = marked.parse(src ?? "", { async: false }) as string;
  return DOMPurify.sanitize(html, {
    // Defaults already drop <script>, on* handlers and javascript: URLs. We additionally refuse the
    // tags that would let a reply reach outside its bubble or phone home.
    FORBID_TAGS: ["style", "form", "input", "button", "iframe", "object", "embed", "link", "meta"],
    FORBID_ATTR: ["style", "srcset", "formaction"],
  });
}
