import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { parseStrictJson } from "../src/strict-json";
import { validateT03Wire } from "../src/t03-wire";

type Fixture = { accept: boolean; id: string; raw: string; schema: string };
const corpus = JSON.parse(readFileSync(
  new URL("../../../tests/fixtures/new-user-t03/strict-cases.json", import.meta.url), "utf8",
)) as { cases: Fixture[] };
const schemas = new Set([
  "contract.describe.response", "search.page.request", "search.page.response",
  "record.read.request", "record.read.response",
]);

describe("production T03 wire grammar", () => {
  for (const fixture of corpus.cases.filter((item) => schemas.has(item.schema))) {
    it(`matches frozen ${fixture.id}`, () => {
      const run = () => validateT03Wire(fixture.schema, parseStrictJson(fixture.raw, true));
      if (fixture.accept) expect(run).not.toThrow();
      else expect(run).toThrow();
    });
  }
});
