import { describe, expect, it } from "vitest";

import { parseReview, safeRelativePath } from "../src/contracts";

describe("plugin result validation", () => {
  it("accepts only contained vault-relative source paths", () => {
    expect(safeRelativePath("Notes/First.md")).toBe(true);
    expect(safeRelativePath("../First.md")).toBe(false);
    expect(safeRelativePath("/private/First.md")).toBe(false);
    expect(safeRelativePath("Notes\\First.md")).toBe(false);
  });

  it("rejects unchecked review endpoints", () => {
    expect(() =>
      parseReview({
        append_text: "[[page_00000000-0000-4000-8000-000000000001]]",
        model: "model",
        provider: "openai_api",
        revision_status: "current",
        source: {
          note_id: "page_00000000-0000-4000-8000-000000000002",
          quote: "evidence",
          relative_path: "../escape.md",
          revision_id: "revision_00000000-0000-4000-8000-000000000003",
        },
        status: "review",
        suggestion_id: "suggestion_00000000-0000-4000-8000-000000000004",
        target: {
          note_id: "page_00000000-0000-4000-8000-000000000005",
          quote: "evidence",
          relative_path: "Target.md",
          revision_id: "revision_00000000-0000-4000-8000-000000000006",
        },
      }),
    ).toThrow("invalid review endpoint");
  });
});
