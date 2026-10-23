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


describe("additive Slack recall responses", () => {
  const baseline = (schema: string): Record<string, unknown> => JSON.parse(
    corpus.cases.find(item => item.accept && item.schema === schema)!.raw,
  ) as Record<string, unknown>;
  const extensions = {
    slack_recall: {
      version: 1, record_read_optional_fields: ["selection"], selection_version: 1,
      maximum_selection_members: 24, error_codes: ["reapproval_required"],
    },
  };
  const frozen = JSON.parse(readFileSync(
    new URL("../../app/tests/fixtures/slack-recall-contracts.v2.json", import.meta.url), "utf8",
  )) as { examples: { read_selection: {
    selection_id: string; selection_version: number; member_count: number; member_ordinal: number;
    members: Array<{ ordinal: number; status: string; record_id?: string; revision_id?: string }>;
  } } };
  const selection = frozen.examples.read_selection;

  it("accepts the exact extension descriptor and preserves old descriptions", () => {
    const original = baseline("contract.describe.response");
    expect(validateT03Wire("contract.describe.response", original)).toBe(original);
    const extended = { ...original, response_extensions: extensions };
    expect(validateT03Wire("contract.describe.response", extended)).toBe(extended);
  });

  it.each([
    {},
    { ...extensions, unrelated: {} },
    { slack_recall: { ...extensions.slack_recall, version: 2 } },
    { slack_recall: { ...extensions.slack_recall, selection_version: 2 } },
    { slack_recall: { ...extensions.slack_recall, maximum_selection_members: 25 } },
    { slack_recall: { ...extensions.slack_recall, record_read_optional_fields: ["selection", "source_id"] } },
    { slack_recall: { ...extensions.slack_recall, error_codes: ["not_found"] } },
    { slack_recall: { ...extensions.slack_recall, extra: true } },
  ])("rejects undeclared or malformed extension %j", response_extensions => {
    expect(() => validateT03Wire("contract.describe.response", {
      ...baseline("contract.describe.response"), response_extensions,
    })).toThrow();
  });

  it("accepts the frozen selection's visible and unavailable members without changing content", () => {
    const original = baseline("record.read.response");
    expect(validateT03Wire("record.read.response", original)).toBe(original);
    const extended = { ...original, selection };
    expect(validateT03Wire("record.read.response", extended)).toBe(extended);
    expect(validateT03Wire("record.read.response", extended).content).toEqual(original.content);
  });

  it("accepts the 24-member boundary", () => {
    const members = Array.from({ length: 24 }, (_, ordinal) => ({ ordinal, status: "unavailable" }));
    members[0] = selection.members[0]!;
    expect(() => validateT03Wire("record.read.response", {
      ...baseline("record.read.response"), selection: { ...selection, member_count: 24, members },
    })).not.toThrow();
  });

  it.each([
    { ...selection, extra: true },
    { ...selection, selection_id: selection.selection_id.replace("-4000-", "-1000-") },
    { ...selection, selection_version: 2 },
    { ...selection, member_count: true },
    { ...selection, member_count: 0, members: [] },
    { ...selection, member_count: 1 },
    { ...selection, member_count: 25, members: Array.from({ length: 25 }, (_, ordinal) => ({ ordinal, status: "unavailable" })) },
    { ...selection, member_ordinal: -1 },
    { ...selection, member_ordinal: 2 },
    { ...selection, members: [...selection.members].reverse() },
    { ...selection, members: [selection.members[0], { ordinal: 0, status: "unavailable" }] },
    { ...selection, members: [selection.members[0], { ordinal: 1, status: "unavailable", record_id: selection.members[0]!.record_id }] },
    { ...selection, members: [selection.members[0], { ordinal: 1, status: "unavailable", source_id: "source_00000000-0000-4000-8000-000000000081" }] },
    { ...selection, members: [{ ...selection.members[0], revision_id: "capture_00000000-0000-4000-8000-000000000087" }, selection.members[1]] },
    { ...selection, members: [{ ...selection.members[0], record_id: "capture_00000000-0000-1000-8000-000000000086", revision_id: "capture_00000000-0000-1000-8000-000000000086" }, selection.members[1]] },
  ])("rejects malformed selection or hidden member identity %j", invalidSelection => {
    expect(() => validateT03Wire("record.read.response", {
      ...baseline("record.read.response"), selection: invalidSelection,
    })).toThrow();
  });

  it("keeps unrelated response fields closed", () => {
    for (const schema of ["contract.describe.response", "record.read.response"]) {
      expect(() => validateT03Wire(schema, { ...baseline(schema), unrelated: true })).toThrow();
    }
    expect(() => validateT03Wire("search.page.response", {
      ...baseline("search.page.response"), selection,
    })).toThrow();
  });
});
