import { readFileSync } from "node:fs";
import { expect, it } from "vitest";
import { parseStrictJson } from "./strict-json";
import { validate } from "../../../tests/fixtures/new-user-t03/validator";
const corpus = JSON.parse(readFileSync(new URL("../../../tests/fixtures/new-user-t03/strict-cases.json", import.meta.url), "utf8"));
for (const fixture of corpus.cases) it(`T03 ${fixture.id}`, () => {
  const run = () => validate(fixture.schema, parseStrictJson(fixture.raw, true));
  if (fixture.accept) expect(run).not.toThrow(); else expect(run).toThrow();
});
it("rejects nested duplicates and invalid UTF-8 before DTO parsing", () => {
  expect(() => parseStrictJson('{"x":{"role":1,"role":2}}')).toThrow();
  expect(() => parseStrictJson(new Uint8Array([0x22, 0xff, 0x22]))).toThrow();
});

const byteCorpus = JSON.parse(readFileSync(new URL("../../../tests/fixtures/new-user-t03/raw-byte-cases.json", import.meta.url), "utf8"));
for (const fixture of byteCorpus.cases) it(`T03 raw bytes ${fixture.id}`, () => {
  const run = () => validate(fixture.schema, parseStrictJson(new Uint8Array(fixture.bytes), true));
  if (fixture.accept) expect(run).not.toThrow(); else expect(run).toThrow();
});
