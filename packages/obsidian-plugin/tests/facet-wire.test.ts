import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { parseHandshake } from "../src/contracts";
import { parseFacetPublicationInspection } from "../src/facet-inspection";
import {
  FACET_OPERATIONS,
  FacetWireError,
  encodeFacetRequest,
  facetLimits,
  negotiateFacets,
  parseFacetRequest,
  parseFacetResponse,
  parseFacetResponseFrame,
  parseManagedRecordRequest,
  parseManagedRecordResponse,
  type FacetOperation,
} from "../src/facet-wire";

type ObjectValue = Record<string, unknown>;
type WireCase = {
  id: string;
  operation: FacetOperation;
  outcome: "accept" | "reject";
  request?: ObjectValue;
  response?: ObjectValue;
  raw_request?: string;
  error_code: string | null;
};
const fixtureRoot = new URL("../../engine/tests/fixtures/facets/", import.meta.url);
const corpus = JSON.parse(readFileSync(new URL("wire-cases.json", fixtureRoot), "utf8")) as {
  cases: WireCase[];
};
const positive = corpus.cases.filter((entry) => entry.outcome === "accept");
const sharedRoot = new URL("fixtures/facets/", import.meta.url);
const shared = JSON.parse(readFileSync(new URL("o0-cases.json", sharedRoot), "utf8")) as {
  contract_revision: string;
  handshake: {
    operations: string[];
    facet_capabilities: {
      version: number;
      operations: { operation: string; dto_versions: number[]; request_bytes: number; response_bytes: number }[];
    };
  };
  managed_request: ObjectValue;
  publication_inspections: ObjectValue[];
  frames: { operation: FacetOperation; result: ObjectValue; frame: string }[];
};
const requestId = "plugin_00000000-0000-4000-8000-000000000001";
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });
const id = (prefix: string, serial = 1) => `${prefix}_00000000-0000-4000-8000-${String(serial).padStart(12, "0")}`;
const object = (value: unknown) => value as ObjectValue;
const array = (value: unknown) => value as unknown[];
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const fixture = (operation: FacetOperation) => {
  const entry = positive.find((candidate) => candidate.operation === operation);
  if (!entry?.request || !entry.response) throw new Error(`missing frozen fixture ${operation}`);
  return { request: copy(entry.request), response: copy(entry.response) };
};
const frame = (result: unknown, overrides: ObjectValue = {}) => JSON.stringify({
  protocol: "open-brain-client", protocol_version: 1, request_id: requestId,
  ok: true, result, ...overrides,
}) + "\n";
const expectCode = (run: () => unknown, code: string) => {
  try {
    run();
    expect.fail(`expected FacetWireError ${code}`);
  } catch (error) {
    expect(error).toBeInstanceOf(FacetWireError);
    expect((error as FacetWireError).code).toBe(code);
  }
};

describe("shared Python frames and captured Stage1 inspection (codec consumption)", () => {
  it("pins the O0 manifest and every schema/corpus fixture input", () => {
    const raw = readFileSync(new URL("manifest.json", sharedRoot));
    expect(createHash("sha256").update(raw).digest("hex")).toBe("bee455084d682e8e717f5f40e1a5cfad82ada7546fdf6412552a8e4844a50d9f");
    const manifest = JSON.parse(raw.toString("utf8")) as {
      contract_revision: string;
      stage_one_pin: { contract_revision: string; fixture_manifest_sha256: string };
      files: Record<string, string>;
    };
    expect(manifest.contract_revision).toBe("obsidian-o0-v1");
    expect(shared.contract_revision).toBe(manifest.contract_revision);
    expect(manifest.stage_one_pin.contract_revision).toBe("facets-c0-v4");
    expect(manifest.stage_one_pin.fixture_manifest_sha256).toBe("95e85b535eeb4263a6e430970ecf1d49de8d0b72de7425e41be703897d93dbc5");
    for (const [path, expected] of Object.entries(manifest.files)) {
      const bytes = readFileSync(new URL(path, new URL("../../../", import.meta.url)));
      expect(createHash("sha256").update(bytes).digest("hex"), path).toBe(expected);
    }
    expect(shared.frames).toHaveLength(23);
    expect(shared.frames.map((entry) => entry.operation).sort()).toEqual([...FACET_OPERATIONS].sort());
  });

  for (const entry of shared.frames) {
    it(`${entry.operation}: decodes the exact Python UTF-8 response frame`, () => {
      const bytes = encoder.encode(entry.frame);
      expect(bytes[bytes.length - 1]).toBe(10);
      expect(bytes.byteLength).toBeLessThanOrEqual(facetLimits(entry.operation).response_bytes);
      expect(JSON.parse(entry.frame)).toEqual({
        protocol: "open-brain-client", protocol_version: 1, request_id: requestId,
        ok: true, result: entry.result,
      });
      expect(parseFacetResponseFrame(entry.operation, bytes, requestId)).toEqual(entry.result);
    });
  }

  it("negotiates the exact Python descriptor for all facets and the resolver", () => {
    // The pinned corpus is historical; this tests its DTOs under current admission.
    const current = { ...shared.handshake, state_schema_version: 22, runtime_session_version: 16 };
    expect(parseHandshake(current)).toEqual(current);
    const result = negotiateFacets(current);
    expect(result.status).toBe("available");
    if (result.status !== "available") throw new Error("expected shared contract");
    expect([...result.operations].sort()).toEqual([...FACET_OPERATIONS, "workspace.record.resolve"].sort());
    for (const entry of shared.handshake.facet_capabilities.operations) {
      expect(entry.dto_versions).toEqual([1]);
      expect(result.limits[entry.operation as keyof typeof result.limits]).toEqual({
        request_bytes: entry.request_bytes, response_bytes: entry.response_bytes,
      });
    }
    expect(parseManagedRecordRequest(shared.managed_request)).toEqual(shared.managed_request);
    const bytes = encodeFacetRequest("workspace.record.resolve", shared.managed_request, requestId);
    expect(object(JSON.parse(decoder.decode(bytes))).arguments).toEqual(shared.managed_request);
  });

  it("accepts the real producer inspection with distinct excerpt and immutable-source digests", () => {
    expect(shared.publication_inspections).toHaveLength(1);
    const value = shared.publication_inspections[0]!;
    const evidence = object(array(value.evidence)[0]);
    const binding = object(value.facet_selection);
    const source = object(array(binding.source_states)[0]);
    expect(evidence.sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(source.sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(evidence.sha256).not.toBe(source.sha256);
    expect(evidence.sha256).toBe(createHash("sha256").update(evidence.excerpt as string).digest("hex"));
    expect(parseFacetPublicationInspection(value)).toEqual(value);
  });
});

describe("frozen C0 v4 codec parity (no server authority proof)", () => {
  it("uses the pinned fixture manifest and exactly all 23 public operations", () => {
    const digest = createHash("sha256").update(readFileSync(new URL("manifest.json", fixtureRoot))).digest("hex");
    expect(digest).toBe("95e85b535eeb4263a6e430970ecf1d49de8d0b72de7425e41be703897d93dbc5");
    expect([...FACET_OPERATIONS].sort()).toEqual(positive.map((entry) => entry.operation).sort());
    expect(FACET_OPERATIONS).toHaveLength(23);
  });

  for (const entry of positive) {
    it(`${entry.operation}: accepts frozen request and response`, () => {
      expect(parseFacetRequest(entry.operation, JSON.stringify(entry.request))).toEqual(entry.request);
      expect(parseFacetResponse(entry.operation, encoder.encode(JSON.stringify(entry.response)))).toEqual(entry.response);
    });
    it(`${entry.operation}: rejects unknown request and response fields`, () => {
      expectCode(() => parseFacetRequest(entry.operation, JSON.stringify({ ...entry.request, owner: true })), "invalid_arguments");
      expect(() => parseFacetResponse(entry.operation, JSON.stringify({ ...entry.response, private_path: "/synthetic" }))).toThrow(FacetWireError);
    });
    it(`${entry.operation}: emits the complete existing plugin frame`, () => {
      const bytes = encodeFacetRequest(entry.operation, entry.request, requestId);
      expect(bytes).toBeInstanceOf(Uint8Array);
      expect(bytes[bytes.length - 1]).toBe(10);
      expect(JSON.parse(decoder.decode(bytes))).toEqual({
        protocol: "open-brain-client", protocol_version: 1, request_id: requestId,
        operation: entry.operation, arguments: entry.request,
      });
      expect(bytes.byteLength).toBeLessThanOrEqual(facetLimits(entry.operation).request_bytes);
      expect(parseFacetResponseFrame(entry.operation, encoder.encode(frame(entry.response)), requestId)).toEqual(entry.response);
    });
  }

  for (const entry of corpus.cases.filter((candidate) => candidate.outcome === "reject")) {
    it(`rejects frozen lexical case ${entry.id}`, () => {
      expectCode(() => parseFacetRequest(entry.operation, entry.raw_request!), entry.error_code!);
    });
  }

  it.each([
    '{"dto_version":1,"limit":9007199254740992}',
    '{"dto_version":1,"limit":1.0}',
    '{"dto_version":1,"limit":1e1}',
    '{"dto_version":1,"cursor":"\\ud800"}',
    '{"dto_version":1,"cursor":"\\u0000"}',
    '{"dto_version":1,"cursor":"\udfff"}',
    '{"dto_version":1} trailing',
    '\u00a0{"dto_version":1}',
    '[]',
  ])("rejects strict request syntax %s", (raw) => {
    expectCode(() => parseFacetRequest("facet.dimension.list", raw), "invalid_arguments");
  });

  it("rejects invalid UTF-8 bytes instead of replacing them", () => {
    const start = encoder.encode('{"dto_version":1,"cursor":"');
    const end = encoder.encode('"}');
    const bytes = new Uint8Array([...start, 0xc0, 0xaf, ...end]);
    expectCode(() => parseFacetRequest("facet.dimension.list", bytes), "invalid_arguments");
  });

  it("expands list defaults and normalizes names and selector sets before counting", () => {
    expect(parseFacetRequest("facet.dimension.list", '{"dto_version":1}')).toEqual({
      dto_version: 1, limit: 50, cursor: null, include_archived: false,
    });
    const create = fixture("facet.dimension.create").request;
    create.name = "  Cafe\u0301  ";
    expect(object(parseFacetRequest("facet.dimension.create", JSON.stringify(create))).name).toBe("Café");
    const query = fixture("facet.query.create").request;
    query.selector = {
      all_value_ids: [id("facetval", 2), ...Array.from({ length: 40 }, () => id("facetval"))],
      space_ids: [id("space", 2), id("space"), id("space")],
      payload_families: ["text", "event", "text"], record_types: ["source", "canonical", "source"],
    };
    expect(object(parseFacetRequest("facet.query.create", JSON.stringify(query))).selector).toEqual({
      all_value_ids: [id("facetval"), id("facetval", 2)], space_ids: [id("space"), id("space", 2)],
      payload_families: ["event", "text"], record_types: ["canonical", "source"],
    });
  });

  it("keeps historical revision UUIDv5 valid while requiring new facet UUIDv4", () => {
    const request = fixture("facet.query.attach").request;
    request.expected_revision_id = "revision_00000000-0000-5000-8000-000000000001";
    expect(parseFacetRequest("facet.query.attach", JSON.stringify(request))).toEqual(request);
    request.query_id = "facetquery_00000000-0000-5000-8000-000000000001";
    expectCode(() => parseFacetRequest("facet.query.attach", JSON.stringify(request)), "invalid_arguments");
  });

  it("rejects patch overlap, stale source revision shape, and empty mutations", () => {
    const request = fixture("facet.assignment.patch").request;
    request.remove_value_ids = request.add_value_ids;
    expectCode(() => parseFacetRequest("facet.assignment.patch", JSON.stringify(request)), "invalid_arguments");
    request.remove_value_ids = [];
    request.expected_revision_id = id("capture", 2);
    expectCode(() => parseFacetRequest("facet.assignment.patch", JSON.stringify(request)), "invalid_arguments");
    request.expected_revision_id = request.record_id;
    request.add_value_ids = [];
    expectCode(() => parseFacetRequest("facet.assignment.patch", JSON.stringify(request)), "invalid_arguments");
  });

  it("requires query page selector XOR saved query with its expected version", () => {
    const request = fixture("facet.query.page").request;
    request.query_id = id("facetquery");
    request.expected_query_version = 1;
    expectCode(() => parseFacetRequest("facet.query.page", JSON.stringify(request)), "invalid_arguments");
    delete request.selector;
    expect(parseFacetRequest("facet.query.page", JSON.stringify(request))).toEqual(request);
    delete request.expected_query_version;
    expectCode(() => parseFacetRequest("facet.query.page", JSON.stringify(request)), "invalid_arguments");
  });

  it("enforces publication create/update target and destination combinations", () => {
    const request = fixture("review.propose_from_facet_query").request;
    request.target_page_id = id("page");
    expectCode(() => parseFacetRequest("review.propose_from_facet_query", JSON.stringify(request)), "invalid_arguments");
    request.expected_page_revision_id = id("revision");
    request.destination_space_id = null;
    expect(parseFacetRequest("review.propose_from_facet_query", JSON.stringify(request))).toEqual(request);
    request.destination_space_id = id("space");
    expectCode(() => parseFacetRequest("review.propose_from_facet_query", JSON.stringify(request)), "invalid_arguments");
  });

  it.each(["order", "duplicates", "selection_version", "pagination"])("rejects noncanonical response %s", (change) => {
    const response = fixture("facet.query.list").response;
    const item = object(array(response.items)[0]);
    const selector = object(item.selector);
    if (change === "order") selector.record_types = ["source", "canonical"];
    if (change === "duplicates") selector.all_value_ids = [id("facetval"), id("facetval")];
    if (change === "selection_version") item.selection_version = 2;
    if (change === "pagination") response.next_cursor = "synthetic-cursor";
    expect(() => parseFacetResponse("facet.query.list", JSON.stringify(response))).toThrow(FacetWireError);
  });

  it.each(["subject", "revision", "membership"])("rejects assignment response mismatch %s", (change) => {
    const response = fixture("facet.assignment.get").response;
    const item = object(response.item);
    if (change === "subject") item.subject = { kind: "canonical", id: id("page") };
    if (change === "revision") item.revision_id = id("capture", 2);
    if (change === "membership") item.memberships = [...array(item.memberships), ...array(item.memberships)];
    expect(() => parseFacetResponse("facet.assignment.get", JSON.stringify(response))).toThrow(FacetWireError);
  });

  it("rejects wrong operation receipt prefix and event/no-change contradictions", () => {
    const response = fixture("facet.dimension.create").response;
    response.subject_id = id("facetval");
    expect(() => parseFacetResponse("facet.dimension.create", JSON.stringify(response))).toThrow(FacetWireError);
    response.subject_id = id("facetdim");
    response.status = "unchanged";
    expect(() => parseFacetResponse("facet.dimension.create", JSON.stringify(response))).toThrow(FacetWireError);
  });

  it("rejects invalid calendar timestamps and imprecise canonical history timestamps", () => {
    const response = fixture("facet.history.list").response;
    const event = object(array(response.items)[0]);
    for (const recorded_at of ["2026-02-30T12:00:00.000000Z", "2026-10-13T12:00:00Z", "2026-10-13T12:00:00.000000+00:00"]) {
      event.recorded_at = recorded_at;
      expect(() => parseFacetResponse("facet.history.list", JSON.stringify(response))).toThrow(FacetWireError);
    }
  });
});

describe("complete plugin frames and operation limits", () => {
  const pageOperations = new Set(["facet.dimension.list", "facet.value.list", "facet.query.list", "facet.query.page", "facet.history.list"]);
  const reads = new Set(["facet.assignment.get", "facet.query.get", "facet.query.attachment.get", "facet.history.state", "facet.vault.preview"]);
  it.each(["facet.dimension.list", "review.propose_from_facet_query"] as const)("bounds the actual raw request bytes for %s", (operation) => {
    const request = fixture(operation).request;
    const raw = JSON.stringify(request);
    const maximum = operation === "review.propose_from_facet_query" ? 1048576 : 65536;
    const padded = raw + " ".repeat(maximum - encoder.encode(raw).byteLength);
    expect(encoder.encode(padded).byteLength).toBe(maximum);
    expect(parseFacetRequest(operation, encoder.encode(padded))).toEqual(request);
    expectCode(() => parseFacetRequest(operation, padded + " "), "invalid_arguments");
  });
  for (const entry of positive) {
    const expectedResponse = pageOperations.has(entry.operation) ? 250000 : reads.has(entry.operation) ? 1048576 : 4096;
    it(`${entry.operation}: has explicit request/response caps and includes final LF`, () => {
      expect(facetLimits(entry.operation)).toEqual({
        request_bytes: entry.operation === "review.propose_from_facet_query" ? 1048576 : 65536,
        response_bytes: expectedResponse,
      });
      const raw = frame(entry.response);
      const padded = raw.slice(0, -1) + " ".repeat(expectedResponse - encoder.encode(raw).byteLength) + "\n";
      expect(encoder.encode(padded).byteLength).toBe(expectedResponse);
      expect(parseFacetResponseFrame(entry.operation, padded, requestId)).toEqual(entry.response);
      expectCode(() => parseFacetResponseFrame(entry.operation, padded + " ", requestId), "response_too_large");
    });
  }

  it("counts UTF-8 bytes in a complete frame, including non-ASCII output", () => {
    const response = fixture("facet.dimension.list").response;
    object(array(response.items)[0]).name = "研究🧠";
    const raw = frame(response);
    const padded = raw.slice(0, -1) + " ".repeat(250000 - encoder.encode(raw).byteLength) + "\n";
    expect(parseFacetResponseFrame("facet.dimension.list", encoder.encode(padded), requestId)).toEqual(response);
    expect(padded.length).toBeLessThan(250000);
    expectCode(() => parseFacetResponseFrame("facet.dimension.list", padded + " ", requestId), "response_too_large");
  });

  it.each(["mismatch", "unknown-field", "float-version", "duplicate-key", "mixed-error"])("rejects outer frame %s", (change) => {
    const response = fixture("facet.dimension.list").response;
    let raw = frame(response);
    if (change === "mismatch") raw = frame(response, { request_id: id("plugin", 2) });
    if (change === "unknown-field") raw = frame(response, { debug: true });
    if (change === "float-version") raw = raw.replace('"protocol_version":1', '"protocol_version":1.0');
    if (change === "duplicate-key") raw = raw.replace('"ok":true', '"ok":true,"ok":true');
    if (change === "mixed-error") raw = frame(response, { error: { code: "not_found" } });
    expect(() => parseFacetResponseFrame("facet.dimension.list", raw, requestId)).toThrow(FacetWireError);
  });

  it.each([
    { name: "malformed", value: "plugin_bad" },
    { name: "UUIDv5", value: id("plugin").replace("4000", "5000") },
    { name: "66000-byte", value: "x".repeat(66000) },
  ])("rejects $name request ID", ({ value }) => {
    expectCode(() => encodeFacetRequest("facet.dimension.create", fixture("facet.dimension.create").request, value), "invalid_arguments");
  });

  it("rejects unknown operations and preserves actual UTF-8 request bodies", () => {
    expect(() => facetLimits("facet.future" as FacetOperation)).toThrow(FacetWireError);
    expect(() => encodeFacetRequest("facet.future" as FacetOperation, {}, requestId)).toThrow(FacetWireError);
    const request = fixture("review.propose_from_facet_query").request;
    request.title = "研究🧠";
    request.markdown = "é".repeat(32768);
    const encoded = encodeFacetRequest("review.propose_from_facet_query", request, requestId);
    expect(object(JSON.parse(decoder.decode(encoded))).arguments).toEqual(request);
    expect(encoded.byteLength).toBeGreaterThan(decoder.decode(encoded).length);
    request.markdown = "é".repeat(32769);
    expectCode(() => encodeFacetRequest("review.propose_from_facet_query", request, requestId), "invalid_arguments");
  });

  it("encodes and correlates the additive managed resolver without advertising it", () => {
    const request = { dto_version: 1, workspace_id: id("workspace"), relative_path: "Notes/研究.md" };
    const response = {
      status: "ok", dto_version: 1, workspace_id: id("workspace"), record_id: id("page"),
      revision_id: "revision_00000000-0000-5000-8000-000000000001", subject: { kind: "canonical", id: id("page") },
    };
    const encoded = encodeFacetRequest("workspace.record.resolve", request, requestId);
    expect(JSON.parse(decoder.decode(encoded))).toEqual({
      protocol: "open-brain-client", protocol_version: 1, request_id: requestId,
      operation: "workspace.record.resolve", arguments: request,
    });
    expect(facetLimits("workspace.record.resolve")).toEqual({ request_bytes: 65536, response_bytes: 1048576 });
    expect(parseFacetResponseFrame("workspace.record.resolve", frame(response), requestId)).toEqual(response);
    expect(() => parseFacetResponseFrame("workspace.record.resolve", frame(response, { request_id: id("plugin", 2) }), requestId)).toThrow(FacetWireError);
  });

  it.each([
    "invalid_arguments", "name_conflict", "not_found", "unsupported_capability", "version_conflict",
    "revision_changed", "idempotency_conflict", "facet_archived", "query_archived", "cursor_stale",
    "cursor_invalid", "limit_exceeded", "response_too_large", "review_conflict", "preview_stale",
    "projection_conflict", "operation_pending",
  ])("preserves closed C0 error code %s", (code) => {
    const raw = JSON.stringify({ protocol: "open-brain-client", protocol_version: 1, request_id: requestId, ok: false, error: { code } }) + "\n";
    expectCode(() => parseFacetResponseFrame("facet.dimension.list", raw, requestId), code);
  });

  it("rejects unrecognized error codes and exception-detail fields", () => {
    for (const error of [{ code: "private_exception" }, { code: "not_found", message: "/synthetic/private/path" }]) {
      const raw = JSON.stringify({ protocol: "open-brain-client", protocol_version: 1, request_id: requestId, ok: false, error }) + "\n";
      expect(() => parseFacetResponseFrame("facet.dimension.list", raw, requestId)).toThrow(FacetWireError);
    }
  });
});

const handshake = (entries?: unknown[], operations: string[] = ["facet.dimension.list", "workspace.record.resolve"]) => ({
  protocol: "open-brain-client", protocol_version: 1, status: "ok", desktop_only: true,
  product_version: "synthetic-o0", operations,
  state_schema_version: 22, runtime_session_version: 16,
  ...(entries === undefined ? {} : { facet_capabilities: { version: 1, operations: entries } }),
});
const capability = (operation = "facet.dimension.list", overrides: ObjectValue = {}) => ({
  operation, dto_versions: [1], request_bytes: 65536,
  response_bytes: operation === "workspace.record.resolve" ? 1048576 : 250000, ...overrides,
});

describe("additive facet negotiation and legacy compatibility", () => {
  it("keeps a current runtime without facet capabilities usable while reporting upgrade_required", () => {
    const value = handshake();
    expect(parseHandshake(value)).toEqual(value);
    expect(negotiateFacets(value)).toEqual({ status: "unavailable", reason: "upgrade_required" });
  });

  it("intersects known descriptor operations with advertised operations without requiring a fixture hash", () => {
    const value = handshake([
      capability(), capability("workspace.record.resolve"), capability("facet.future"),
      capability("facet.query.page"),
    ]);
    expect(parseHandshake(value)).toEqual(value);
    const result = negotiateFacets(value);
    expect(result.status).toBe("available");
    if (result.status !== "available") throw new Error("expected additive contract");
    expect([...result.operations].sort()).toEqual(["facet.dimension.list", "workspace.record.resolve"]);
    expect(result.limits).toEqual({
      "facet.dimension.list": { request_bytes: 65536, response_bytes: 250000 },
      "workspace.record.resolve": { request_bytes: 65536, response_bytes: 1048576 },
    });
  });

  it("disables only the unsupported DTO operation and accepts lower negotiated caps", () => {
    const value = handshake([capability(undefined, { dto_versions: [2] }), capability("workspace.record.resolve", { request_bytes: 4096, response_bytes: 8192 })]);
    const result = negotiateFacets(value);
    expect(result.status).toBe("available");
    if (result.status !== "available") throw new Error("expected resolver contract");
    expect(result.operations).toEqual(["workspace.record.resolve"]);
    expect(result.limits).toEqual({ "workspace.record.resolve": { request_bytes: 4096, response_bytes: 8192 } });
  });

  it.each([
    { duplicate: true }, { extra: true }, { request_bytes: 65537 }, { response_bytes: 250001 },
    { request_bytes: 0 }, { request_bytes: -1 }, { response_bytes: 1.5 },
    { dto_versions: [true] }, { request_bytes: Number.NaN }, { response_bytes: Number.MAX_SAFE_INTEGER + 1 },
  ])("fails closed for malformed descriptors %j", (change) => {
    const entries = change.duplicate ? [capability(), capability()] : [capability(undefined, change.extra ? { owner: true } : change)];
    expect(negotiateFacets(handshake(entries))).toEqual({ status: "unavailable", reason: "incompatible" });
  });

  it.each([
    { version: 2, operations: [capability()] },
    { version: 1, operations: [capability()], fixture_hash: "untrusted" },
    { version: 1.5, operations: [capability()] },
  ])("rejects incompatible closed capability headers %j", (facet_capabilities) => {
    expect(negotiateFacets({ ...handshake(), facet_capabilities })).toEqual({ status: "unavailable", reason: "incompatible" });
  });
});

describe("managed record identity grammar (not custody or source mapping proof)", () => {
  const request = { dto_version: 1, workspace_id: id("workspace"), relative_path: "Notes/研究.md" };
  const canonical = {
    status: "ok", dto_version: 1, workspace_id: id("workspace"), record_id: id("page"),
    revision_id: "revision_00000000-0000-5000-8000-000000000001", subject: { kind: "canonical", id: id("page") },
  };
  it("accepts normalized Markdown paths and does not infer generated-index custody from a folder name", () => {
    expect(parseManagedRecordRequest(request)).toEqual(request);
    expect(parseManagedRecordRequest({ ...request, relative_path: "Facets/synthetic.md" })).toEqual({ ...request, relative_path: "Facets/synthetic.md" });
    expect(parseManagedRecordResponse(canonical)).toEqual(canonical);
  });

  it.each(["/Notes/a.md", "../a.md", "Notes/../a.md", "Notes\\a.md", "Notes//a.md", "./a.md", "Notes/./a.md", "", "Notes/a.txt", "Notes/a\u0000.md", "Notes/a\n.md"])("rejects invalid relative path %s", (relative_path) => {
    expect(() => parseManagedRecordRequest({ ...request, relative_path })).toThrow();
  });

  it("rejects supplied source metadata and unknown fields", () => {
    expect(() => parseManagedRecordRequest({ ...request, source_id: id("source") })).toThrow();
    expect(() => parseManagedRecordResponse({ ...canonical, owner: true })).toThrow();
  });

  it("accepts a coherent codec-only source response and rejects all identity mismatches", () => {
    const source = { ...canonical, record_id: id("capture"), revision_id: id("capture"), subject: { kind: "source", id: id("source") } };
    expect(parseManagedRecordResponse(source)).toEqual(source);
    for (const invalid of [
      { ...source, revision_id: id("capture", 2) },
      { ...source, subject: { kind: "source", id: id("page") } },
      { ...canonical, subject: { kind: "canonical", id: id("page", 2) } },
      { ...canonical, revision_id: id("capture") },
      { ...canonical, workspace_id: id("space") },
    ]) expect(() => parseManagedRecordResponse(invalid)).toThrow();
  });
});

const inspection = (name: "review-create-cross-space" | "review-update") => {
  const binding = JSON.parse(readFileSync(new URL(`positive/${name}.json`, fixtureRoot), "utf8")) as ObjectValue;
  return {
    status: "shown", proposal_status: "pending", proposal_id: binding.proposal_id,
    title: "Synthetic facet draft", markdown: "# Synthetic facet draft\n",
    space_id: binding.destination_space_id, page_id: binding.page_id,
    target_page_id: binding.operation === "update" ? binding.page_id : null,
    operation: binding.operation, capture_ids: binding.provenance,
    selected_capture_ids: binding.selected_capture_ids,
    evidence: array(binding.source_states).map((entry) => {
      const state = object(entry);
      return { capture_id: state.capture_id, excerpt: "Synthetic evidence", sha256: state.sha256, projection_applied: false };
    }),
    review_token: binding.review_digest, expected_page_sha256: binding.expected_page_sha256,
    expected_publication_id: binding.expected_publication_id, projection_applied: false,
    facet_selection: binding,
  };
};

describe("full facet publication inspection (pure frozen binding codec)", () => {
  const developmentInspection = () => {
    const value = inspection("review-create-cross-space");
    value.facet_selection.schema_version = 6;
    value.facet_selection.base_schema_version = 5;
    value.facet_selection.development_evidence = [
      ...array(value.facet_selection.provenance).map((capture_id) => ({
        capture_id, evidence_sha256: "d".repeat(64),
      })),
      { capture_id: id("capture", 99), evidence_sha256: null },
    ].sort((a, b) => String(a.capture_id).localeCompare(String(b.capture_id)));
    return value;
  };

  it("accepts development-backed facet confirmation without dropping commitments", () => {
    const value = developmentInspection();
    expect(parseFacetPublicationInspection(value)).toEqual(value);
  });

  it.each(["base", "missing", "extra", "digest", "duplicate", "order", "leaf", "uncovered"])("rejects malformed development commitment %s", (change) => {
    const value = developmentInspection();
    const binding = value.facet_selection;
    const commitments = array(binding.development_evidence);
    if (change === "base") binding.base_schema_version = 4;
    if (change === "missing") delete binding.development_evidence;
    if (change === "extra") object(commitments[0]).owner = true;
    if (change === "digest") object(commitments[0]).evidence_sha256 = "invalid";
    if (change === "duplicate") commitments.push(commitments[0]);
    if (change === "order") binding.development_evidence = [...commitments].reverse();
    if (change === "leaf") object(commitments[0]).capture_id = "invalid";
    if (change === "uncovered") binding.development_evidence = [{ capture_id: id("capture", 99), evidence_sha256: null }];
    expect(() => parseFacetPublicationInspection(value)).toThrow();
  });

  it.each(["review-create-cross-space", "review-update"] as const)("accepts %s with selected/inherited distinction intact", (name) => {
    const value = inspection(name);
    expect(parseFacetPublicationInspection(value)).toEqual(value);
    if (name === "review-update") {
      expect(value.capture_ids).toHaveLength(2);
      expect(value.selected_capture_ids).toHaveLength(1);
      expect(array(value.facet_selection.facet_source_states)).toHaveLength(1);
      expect(array(value.facet_selection.source_states)).toHaveLength(2);
    }
  });

  it.each(["review_token", "proposal_id", "page_id", "space_id", "operation", "target_page_id", "expected_page_sha256", "expected_publication_id", "capture_ids", "selected_capture_ids"])("rejects outer/binding mismatch %s", (field) => {
    const value = object(inspection("review-update"));
    if (field === "review_token" || field === "expected_page_sha256") value[field] = "e".repeat(64);
    else if (field === "operation") value[field] = "create";
    else if (field === "capture_ids" || field === "selected_capture_ids") value[field] = [id("capture", 99)];
    else value[field] = id(field === "space_id" ? "space" : field === "expected_publication_id" ? "publication" : field === "proposal_id" ? "proposal" : "page", 99);
    expect(() => parseFacetPublicationInspection(value)).toThrow();
  });

  it.each(["outer", "binding", "selector", "source", "membership", "patch", "missing-binding"])("rejects closed inspection mutation %s", (change) => {
    const value = object(inspection("review-create-cross-space"));
    const binding = object(value.facet_selection);
    if (change === "outer") value.owner = true;
    if (change === "binding") binding.owner = true;
    if (change === "selector") object(binding.selector).owner = true;
    if (change === "source") object(array(binding.facet_source_states)[0]).private_path = "/synthetic";
    if (change === "membership") object(array(object(array(binding.facet_source_states)[0]).memberships)[0]).private_version = 1;
    if (change === "patch") { value.patch = {}; value.patch_diff = "synthetic"; }
    if (change === "missing-binding") delete value.facet_selection;
    expect(() => parseFacetPublicationInspection(value)).toThrow();
  });

  it.each(["set-order", "membership-order", "membership-zero", "head-zero", "selection-version", "attachment", "source-state", "evidence"])("rejects malformed frozen state %s", (change) => {
    const value = inspection("review-create-cross-space");
    const binding = value.facet_selection;
    const state = object(array(binding.facet_source_states)[0]);
    const memberships = array(state.memberships);
    if (change === "set-order") object(binding.selector).all_value_ids = array(object(binding.selector).all_value_ids).reverse();
    if (change === "membership-order") state.memberships = memberships.reverse();
    if (change === "membership-zero") object(memberships[0]).membership_version = 0;
    if (change === "head-zero") state.head_version = 0;
    if (change === "selection-version") binding.query_selection_version = 4;
    if (change === "attachment") binding.attachment_version = 1;
    if (change === "source-state") object(array(binding.source_states)[0]).capture_id = id("capture", 99);
    if (change === "evidence") value.evidence[0]!.sha256 = "not-a-digest";
    expect(() => parseFacetPublicationInspection(value)).toThrow();
  });
});
