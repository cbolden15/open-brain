import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { parseStrictJson } from "./strict-json";
import { parseRecordSummary, validateT03Wire } from "./t03-wire";

type Fixture = { accept: boolean; id: string; raw: string; schema: string };
const corpus = JSON.parse(readFileSync(
  new URL("../../../tests/fixtures/new-user-t03/strict-cases.json", import.meta.url), "utf8",
)) as { cases: Fixture[] };
const schemas = new Set([
  "contract.describe.response", "search.page.request", "search.page.response",
  "record.read.request", "record.read.response",
]);

describe("desktop production T03 wire grammar", () => {
  for (const fixture of corpus.cases.filter(item => schemas.has(item.schema))) {
    it(`matches frozen ${fixture.id}`, () => {
      const run = () => validateT03Wire(fixture.schema, parseStrictJson(fixture.raw, true));
      if (fixture.accept) expect(run).not.toThrow();
      else expect(run).toThrow();
    });
  }

  it("preserves ordered provenance and valid empty projected strings", () => {
    const summary = parseRecordSummary({
      record_id: "page_123e4567-e89b-42d3-a456-426614174400",
      record_type: "canonical",
      revision_id: "revision_123e4567-e89b-42d3-a456-426614174600",
      source_id: null,
      payload_family: "event",
      space_id: null,
      title: "",
      excerpt: "",
      trust: "reviewed",
      provenance: {
        representative_capture_id: "capture_123e4567-e89b-42d3-a456-426614174100",
        capture_ids: [
          "capture_123e4567-e89b-42d3-a456-426614174100",
          "capture_123e4567-e89b-42d3-a456-426614174101",
        ],
        source_origin: "mixed",
      },
      source_update_available: false,
    });
    expect(summary.provenance.capture_ids).toEqual([
      "capture_123e4567-e89b-42d3-a456-426614174100",
      "capture_123e4567-e89b-42d3-a456-426614174101",
    ]);
    expect(summary.title).toBe("");
    expect(summary.excerpt).toBe("");
  });
});
