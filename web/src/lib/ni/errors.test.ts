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
