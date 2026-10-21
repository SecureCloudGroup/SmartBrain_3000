import { describe, expect, it } from "vitest";
import { friendlyErrorClass } from "./errors";

describe("friendlyErrorClass", () => {
  it("maps every documented class to its human sentence", () => {
    expect(friendlyErrorClass("extract_miss")).toBe("couldn't find the expected data");
    expect(friendlyErrorClass("fetch_failed")).toBe("couldn't reach the source");
    expect(friendlyErrorClass("image_type")).toBe("not a supported image");
    expect(friendlyErrorClass("llm_requires_local")).toBe("needs a local model");
    expect(friendlyErrorClass("mcp_unavailable")).toBe("your MCP server didn't answer");
    expect(friendlyErrorClass("secret_missing")).toBe("credential problem");
    expect(friendlyErrorClass("secret_host_mismatch")).toBe("credential problem");
  });

  // Round 20 (B3a, ni-format §35): the page browser's closed failure set (ni._RENDER_FAILURES —
  // the runner's refused / timeout / blocked / offsite / confinement / oversize / engine_error).
  it("names every page-browser failure class in its own plain words", () => {
    const words: Record<string, string> = {
      render_refused: "the page browser wouldn't open this address",
      render_timeout: "the page took too long in the page browser",
      render_blocked: "the site turned the page browser away",
      render_offsite: "the page moved to a site this card doesn't allow",
      render_confinement: "the page browser's safety checks stopped the read",
      render_oversize: "the page was too big for the page browser",
      render_engine: "the page browser couldn't read the page",
    };
    for (const [cls, phrase] of Object.entries(words)) expect(friendlyErrorClass(cls), cls).toBe(phrase);
    expect(new Set(Object.values(words)).size).toBe(7);
  });

  it("explains a card that needs consent for a model off this computer", () => {
    expect(friendlyErrorClass("model_consent_required")).toBe(
      "needs your OK to use a model off this computer; rebuild the card to choose",
    );
  });

  it("echoes an unknown class verbatim (no invented story)", () => {
    expect(friendlyErrorClass("contract_violation")).toBe("contract_violation");
    expect(friendlyErrorClass("weird_new_thing")).toBe("weird_new_thing");
  });

  it("returns empty for null / undefined / blank input", () => {
    expect(friendlyErrorClass(null)).toBe("");
    expect(friendlyErrorClass(undefined)).toBe("");
    expect(friendlyErrorClass("")).toBe("");
    expect(friendlyErrorClass("   ")).toBe("");
  });
});
