import { BridgeError } from "./bridge";

type Rule = {
  anyOf?: Rule[]; enum?: unknown[]; format?: "query"; idPrefixes?: string[];
  items?: Rule; max?: number; min?: number; optional?: string[];
  properties?: Record<string, Rule>; ref?: string; type?: "array" | "boolean" | "integer" | "null" | "object" | "string";
  unique?: boolean;
};

const id = (idPrefixes: string[]): Rule => ({ type: "string", idPrefixes });
const cursor: Rule = { anyOf: [{ type: "null" }, { type: "string", min: 1, max: 128 }] };
const provenance: Rule = { type: "object", optional: [], properties: {
  representative_capture_id: id(["capture"]),
  capture_ids: { type: "array", items: id(["capture"]), min: 1, max: 32, unique: true },
  source_origin: { enum: ["owner_authored", "third_party", "mixed", "unknown"] },
} };
const summary: Rule = { type: "object", optional: [], properties: {
  record_id: id(["capture", "page"]), record_type: { enum: ["source", "canonical"] },
  revision_id: id(["capture", "revision"]), source_id: { anyOf: [{ type: "null" }, id(["source"])] },
  payload_family: { enum: ["text", "event", "measurement", "reference_or_file"] },
  space_id: { anyOf: [{ type: "null" }, id(["space"])] }, title: { type: "string" },
  excerpt: { type: "string" }, trust: { enum: ["owner", "third_party", "reviewed", "unverified"] },
  provenance, source_update_available: { type: "boolean" },
} };
const filters: Rule = { type: "object", optional: [], properties: {
  space_ids: { type: "array", items: id(["space"]), min: 0, max: 100, unique: true },
  payload_families: { type: "array", items: { enum: ["text", "event", "measurement", "reference_or_file"] }, min: 0, max: 4, unique: true },
  record_types: { type: "array", items: { enum: ["source", "canonical"] }, min: 0, max: 2, unique: true },
} };

const schemas: Record<string, Rule> = {
  "contract.describe.response": { type: "object", optional: [], properties: {
    status: { enum: ["ok"] }, contract_version: { enum: ["t03.v1"] },
    operations: { type: "array", min: 0, max: 5, unique: true, items: { type: "object", optional: [], properties: {
      name: { enum: ["search.page", "record.read", "history.list", "history.show", "source.route"] },
      dto_version: { enum: [1] }, required_grants: { type: "array", min: 1, max: 1, unique: true,
        items: { enum: ["search", "content-read", "history-read", "organize"] } },
    } } },
    limits: { type: "object", optional: [], properties: { request_bytes: { enum: [65536] }, response_bytes: { enum: [1048576] },
      content_calls: { enum: [500] }, content_bytes: { enum: [16777216] }, history_calls: { enum: [500] }, history_bytes: { enum: [16777216] } } },
  } },
  "search.page.request": { type: "object", optional: ["filters", "mode", "limit", "cursor"], properties: {
    dto_version: { enum: [1] }, query: { type: "string", min: 1, max: 500, format: "query" }, filters,
    mode: { enum: ["lexical", "hybrid_preferred", "hybrid_required"] }, limit: { type: "integer", min: 1, max: 100 }, cursor,
  } },
  "search.page.response": { type: "object", optional: [], properties: {
    status: { enum: ["ok"] }, dto_version: { enum: [1] }, next_cursor: cursor, complete: { type: "boolean" },
    results: { type: "array", items: summary, min: 0, max: 100 }, mode_used: { enum: ["lexical", "hybrid"] },
    warnings: { type: "array", items: { enum: ["model_unavailable", "projection_stale"] }, min: 0, max: 1, unique: true },
  } },
  "record.read.request": { type: "object", optional: ["target_bytes", "cursor"], properties: {
    dto_version: { enum: [1] }, record_id: id(["capture", "page"]), expected_revision_id: id(["capture", "revision"]),
    target_bytes: { type: "integer", min: 1, max: 65536 }, cursor,
  } },
  "record.read.response": { type: "object", optional: [], properties: {
    status: { enum: ["ok"] }, dto_version: { enum: [1] }, next_cursor: cursor, complete: { type: "boolean" }, record: summary,
    content: { type: "object", optional: [], properties: { kind: { enum: ["untrusted_text"] }, text: { type: "string" } } },
    start_byte: { type: "integer", min: 0, max: Number.MAX_SAFE_INTEGER }, end_byte: { type: "integer", min: 0, max: Number.MAX_SAFE_INTEGER },
  } },
};

const grants: Record<string, string> = { "search.page": "search", "record.read": "content-read", "history.list": "history-read", "history.show": "history-read", "source.route": "organize" };

export function validateT03Wire(name: keyof typeof schemas, value: unknown): Record<string, unknown> {
  try { check(schemas[name]!, value); } catch { throw new BridgeError("protocol_error"); }
  return value as Record<string, unknown>;
}

function check(rule: Rule, value: unknown): void {
  if (rule.anyOf !== undefined) {
    for (const option of rule.anyOf) { try { check(option, value); return; } catch { /* try next */ } }
    throw new Error("invalid");
  }
  if (rule.enum !== undefined) { require(rule.enum.some((item) => item === value)); return; }
  switch (rule.type) {
    case "null": require(value === null); return;
    case "boolean": require(typeof value === "boolean"); return;
    case "integer": require(Number.isSafeInteger(value) && Number(value) >= rule.min! && Number(value) <= rule.max!); return;
    case "string": {
      require(typeof value === "string");
      const scalars = [...value];
      require(scalars.every((character) => character.codePointAt(0) !== 0 && !isSurrogate(character.codePointAt(0)!)));
      require(scalars.length >= (rule.min ?? 0) && scalars.length <= (rule.max ?? 1_048_576));
      if (rule.idPrefixes !== undefined) require(new RegExp(`^(?:${rule.idPrefixes.join("|")})_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`).test(value));
      if (rule.format === "query") require(/[\p{L}\p{N}]/u.test(value) && new TextEncoder().encode(value).length <= 4096);
      return;
    }
    case "array": {
      require(Array.isArray(value) && value.length >= rule.min! && value.length <= rule.max!);
      if (rule.unique === true) require(new Set(value.map((item) => JSON.stringify(item))).size === value.length);
      value.forEach((item) => check(rule.items!, item)); return;
    }
    case "object": {
      require(value !== null && typeof value === "object" && !Array.isArray(value));
      const item = value as Record<string, unknown>;
      const properties = rule.properties!;
      require(Object.keys(item).every((key) => Object.prototype.hasOwnProperty.call(properties, key)));
      require(Object.keys(properties).every((key) => rule.optional!.includes(key) || Object.prototype.hasOwnProperty.call(item, key)));
      for (const [key, child] of Object.entries(item)) check(properties[key]!, child);
      if ("complete" in item) require(item.complete === (item.next_cursor === null));
      if ("representative_capture_id" in item) {
        require(Array.isArray(item.capture_ids) && item.representative_capture_id === item.capture_ids[0]);
      }
      if ("record_type" in item) checkRecordConsistency(item);
      if ("content" in item) {
        const content = item.content as { text: string };
        const length = new TextEncoder().encode(content.text).length;
        require(Number(item.end_byte) - Number(item.start_byte) === length && (item.complete === true || length > 0));
      }
      if ("required_grants" in item) {
        require(Array.isArray(item.required_grants) && item.required_grants[0] === grants[String(item.name)]);
      }
      if ("operations" in item) {
        const operations = item.operations as Array<{ name: string }>;
        require(new Set(operations.map((operation) => operation.name)).size === operations.length);
      }
      return;
    }
    default: throw new Error("invalid schema");
  }
}

function checkRecordConsistency(item: Record<string, unknown>): void {
  const provenanceItem = item.provenance as { capture_ids: string[] };
  if (item.record_type === "source") {
    require(String(item.record_id).startsWith("capture_") && item.record_id === item.revision_id && item.source_id !== null);
    require(provenanceItem.capture_ids.length === 1 && provenanceItem.capture_ids[0] === item.record_id);
  } else {
    require(String(item.record_id).startsWith("page_") && String(item.revision_id).startsWith("revision_") && item.source_id === null);
  }
}

function isSurrogate(value: number): boolean { return value >= 0xd800 && value <= 0xdfff; }
function require(condition: unknown): asserts condition { if (!condition) throw new Error("invalid"); }
