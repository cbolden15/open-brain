import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { parseStrictJson } from "../src/strict-json";
import { parseHandshake, parseSearch } from "../src/contracts";
import { validate } from "../../../tests/fixtures/new-user-t03/validator";
const corpus = JSON.parse(readFileSync(new URL("../../../tests/fixtures/new-user-t03/strict-cases.json", import.meta.url), "utf8"));
describe("T03 strict contract", () => {
  for (const fixture of corpus.cases) it(fixture.id, () => {
    const run = () => validate(fixture.schema, parseStrictJson(fixture.raw, true));
    if (fixture.accept) expect(run).not.toThrow(); else expect(run).toThrow();
  });
  it("rejects nested duplicate and invalid UTF-8 at the transport boundary", () => {
    expect(() => parseStrictJson('{"x":{"role":1,"role":2}}')).toThrow();
    expect(() => parseStrictJson(new Uint8Array([0x22, 0xff, 0x22]))).toThrow();
  });
  it("retains actual legacy parsing and old-server capability fallback", () => {
    const handshake = parseHandshake({status:"ok", desktop_only:true, protocol:"open-brain-client", protocol_version:1, product_version:"0.1.0", operations:["search.query"]});
    expect(handshake.operations).not.toContain("contract.describe");
    expect(parseSearch({status:"ok", results:[]})).toEqual({status:"ok", results:[]});
    expect(() => parseSearch({status:"ok", results:[{record_id:"future"}]})).toThrow();
  });
  it("consumes the exact legacy Python producer fixture", () => {
    const old = JSON.parse(readFileSync(new URL("../../../tests/fixtures/new-user-t03/compatibility.json", import.meta.url), "utf8"));
    const result = parseSearch(old.baseline_search_serialization.expected);
    expect(result.results[0]?.result_id).toBe(old.baseline_search_serialization.input.result_id);
  });

});

const byteCorpus = JSON.parse(readFileSync(new URL("../../../tests/fixtures/new-user-t03/raw-byte-cases.json", import.meta.url), "utf8"));
for (const fixture of byteCorpus.cases) it(`T03 raw bytes ${fixture.id}`, () => {
  const run = () => validate(fixture.schema, parseStrictJson(new Uint8Array(fixture.bytes), true));
  if (fixture.accept) expect(run).not.toThrow(); else expect(run).toThrow();
});
