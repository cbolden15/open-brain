/** O0 codecs only. Live bridge dispatch and UI registration belong to Stage 2 B. */
import schema from "../../engine/src/open_brain_engine/engine/facet_schema.json";
import { parseStrictJson } from "./strict-json";
import { parseHandshake } from "./contracts";
import type { FacetOperation, FacetRequestMap, FacetResponseMap } from "./facet-types";

export type { FacetOperation, FacetRequestMap, FacetResponseMap } from "./facet-types";
export const FACET_OPERATIONS = Object.freeze(Object.keys(schema.operations) as FacetOperation[]);
export const RESOLVE_OPERATION = "workspace.record.resolve";
export type FacetBridgeOperation = FacetOperation | typeof RESOLVE_OPERATION;
export interface FacetLimits { request_bytes: number; response_bytes: number }
export class FacetWireError extends Error {
  constructor(public readonly code: string) { super(code); this.name = "FacetWireError"; }
}
export function requireFacet(condition: unknown, code = "invalid_arguments"): asserts condition {
  if (!condition) throw new FacetWireError(code);
}
// The schema is trusted build input; untrusted values pass this interpreter before typed use.
type Rule = Record<string, any>;
const definitions: Record<string, Rule> = schema.definitions;
const own = (value: object, key: string): boolean => Object.prototype.hasOwnProperty.call(value, key);
export function object(value: unknown): Record<string, any> {
  requireFacet(typeof value === "object" && value !== null && !Array.isArray(value));
  return value;
}
export function exact(value: unknown, required: readonly string[], optional: readonly string[] = []): Record<string, any> {
  const item = object(value);
  requireFacet(required.every((key) => own(item, key)) && Object.keys(item).every((key) => required.includes(key) || optional.includes(key)));
  return item;
}
export function identifier(value: unknown, prefixes: readonly string[], facet = false): value is string {
  const uuid = facet ? "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}" : "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
  return typeof value === "string" && new RegExp(`^(?:${prefixes.join("|")})_${uuid}$`).test(value);
}
export const safeInteger = (value: unknown, min = 0): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= min;
export const digest = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const controls = /[\u0000-\u001f\u007f-\u009f]/u;
const pythonTrim = (value: string): string => value.replace(/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu, "");
const size = (value: string): number => new TextEncoder().encode(value).length;
const stable = (value: any): string => JSON.stringify(value, (_key, item) => item && !Array.isArray(item) && typeof item === "object" ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item);
export function timestamp(value: unknown): boolean {
  if (typeof value !== "string" || !/^[0-9]{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$/.test(value) || value.startsWith("0000")) return false;
  const date = new Date(value);
  return !Number.isNaN(date.valueOf()) && date.toISOString() === value.slice(0, 23) + "Z";
}

function semantics(v: Record<string, any>, rule?: string): void {
  if (rule === "assignment_patch") {
    requireFacet(v.add_value_ids.length + v.remove_value_ids.length > 0 && !v.add_value_ids.some((id: string) => v.remove_value_ids.includes(id)));
    const source = v.record_id.startsWith("capture_");
    requireFacet(v.expected_revision_id.startsWith(source ? "capture_" : "revision_") && (!source || v.expected_revision_id === v.record_id));
  }
  if (rule === "query_page") requireFacet(own(v, "selector") !== own(v, "query_id") && own(v, "query_id") === own(v, "expected_query_version"));
  if (rule === "publication") requireFacet((v.target_page_id === null) === (v.expected_page_revision_id === null) && (v.target_page_id === null) === (v.destination_space_id !== null));
  if (own(v, "complete")) requireFacet(v.complete === (v.next_cursor === null));
  if (["applied", "unchanged"].includes(v.status)) requireFacet((v.event_id === null) === (v.status === "unchanged"));
  if (own(v, "effective_archived")) requireFacet(!v.archived || v.effective_archived);
  if (own(v, "selection_version")) requireFacet(v.selection_version <= v.version);
  if (own(v, "subject_kind")) requireFacet(v.subject_id.startsWith(v.subject_kind === "source" ? "source_" : "page_"));
  if (own(v, "attachment") && own(v, "subject")) requireFacet(v.subject.kind === "source" ? v.attachment === null : v.attachment === null || v.attachment.page_id === v.subject.id);
  if (own(v, "memberships")) requireFacet(new Set(v.memberships.map((x: any) => x.value_id)).size === v.memberships.length);
  if (own(v, "subject") && own(v, "record_id")) {
    const source = v.subject.kind === "source";
    requireFacet(v.record_id.startsWith(source ? "capture_" : "page_") && v.revision_id.startsWith(source ? "capture_" : "revision_"));
    requireFacet(source ? v.record_id === v.revision_id : v.record_id === v.subject.id);
  }
  if (own(v, "record") && own(v, "classification_version")) requireFacet(v.record.record_type === v.subject.kind && v.subject.id === (v.subject.kind === "source" ? v.record.source_id : v.record.record_id));
  if (own(v, "representative_capture_id")) requireFacet(v.representative_capture_id === v.capture_ids[0]);
  if (own(v, "record_type")) {
    const source = v.record_type === "source";
    requireFacet(v.record_id.startsWith(source ? "capture_" : "page_") && v.revision_id.startsWith(source ? "capture_" : "revision_") && (v.source_id !== null) === source);
    if (source) requireFacet(v.record_id === v.revision_id && stable(v.provenance.capture_ids) === stable([v.record_id]));
  }
  if (own(v, "entity") && own(v, "after")) {
    for (const row of [v.before, v.after]) {
      if (row === null) continue;
      const kind = v.entity.kind;
      const identity = kind === "subject" ? { kind: row.subject_kind, id: row.subject_id } : kind === "assignment" ? { subject_kind: row.subject_kind, subject_id: row.subject_id, value_id: row.value_id } : row[({ dimension: "dimension_id", value: "value_id", query: "query_id", attachment: "page_id" } as Record<string, string>)[kind]!];
      requireFacet(stable(identity) === stable(v.entity.id));
    }
  }
  if (own(v, "conflicts")) for (const conflict of v.conflicts) if (["unknown_name", "ambiguous_name"].includes(conflict.code)) requireFacet(conflict.value_id === null);
  if (v.status === "proposed") requireFacet((v.operation === "create") === (v.target_page_id === null) && (v.operation === "create" || v.page_id === v.target_page_id));
}

export function validateFacetDefinition(name: string, value: unknown): any {
  const normalize = name.endsWith(".request") || name === "selector.input";
  function check(rule: Rule, child: any): any {
    if (rule.ref) { requireFacet(own(definitions, rule.ref)); return check(definitions[rule.ref]!, child); }
    if (rule.anyOf) {
      for (const option of rule.anyOf) { try { return check(option, child); } catch (error) { if (!(error instanceof FacetWireError)) throw error; } }
      throw new FacetWireError("invalid_arguments");
    }
    if (rule.enum) { requireFacet(rule.enum.some((item: unknown) => item === child)); return child; }
    if (rule.type === "object") {
      const item = exact(child, Object.keys(rule.properties).filter((key) => !rule.optional.includes(key)), rule.optional);
      const expanded = normalize ? { ...rule.defaults, ...item } : item;
      const result = Object.fromEntries(Object.entries(expanded).map(([key, item]) => [key, check(rule.properties[key], item)]));
      semantics(result, rule.semantic); return result;
    }
    if (rule.type === "array") {
      requireFacet(Array.isArray(child));
      let result = child.map((item) => check(rule.items, item));
      if (normalize && rule.normalize_set) result = [...new Set(result)].sort();
      requireFacet(result.length >= rule.min && result.length <= rule.max);
      if (rule.unique) requireFacet(new Set(result.map(stable)).size === result.length);
      if (rule.sorted) requireFacet(stable(result) === stable([...result].sort()));
      return result;
    }
    if (rule.type === "integer") { requireFacet(safeInteger(child, rule.min) && child <= rule.max); return child; }
    if (rule.type === "boolean") { requireFacet(typeof child === "boolean"); return child; }
    if (rule.type === "null") { requireFacet(child === null); return child; }
    requireFacet(rule.type === "string" && typeof child === "string");
    requireFacet(!Array.from(child).some((c) => c === "\0" || (c.codePointAt(0)! >= 0xd800 && c.codePointAt(0)! <= 0xdfff)));
    if (rule.format === "name") {
      requireFacet(!controls.test(child));
      const normalized = pythonTrim(child.normalize("NFC"));
      if (normalize) child = normalized; else requireFacet(child === normalized);
    }
    if (["name", "operation_id", "title"].includes(rule.format)) requireFacet(pythonTrim(child).length > 0 && !controls.test(child));
    requireFacet(Array.from(child).length >= (rule.min ?? 0) && Array.from(child).length <= (rule.max ?? 1048576) && size(child) <= (rule.max_bytes ?? 1048576));
    if (rule.id_prefixes) requireFacet(identifier(child, rule.id_prefixes, /^(facetdim|facetval|facetquery|facetevent)_/.test(child)));
    if (rule.format === "timestamp") requireFacet(timestamp(child));
    return child;
  }
  requireFacet(own(definitions, name));
  return check(definitions[name]!, value);
}

const isOperation = (value: string): value is FacetOperation => own(schema.operations, value);
function strict(raw: string | Uint8Array): unknown {
  try { return parseStrictJson(raw, true); } catch { throw new FacetWireError("invalid_arguments"); }
}
export function parseFacetRequest<O extends FacetOperation>(operation: O, raw: string | Uint8Array): FacetRequestMap[O] {
  requireFacet(isOperation(operation));
  requireFacet((typeof raw === "string" ? size(raw) : raw.length) <= facetLimits(operation).request_bytes);
  return validateFacetDefinition(operation + ".request", strict(raw));
}
export function parseFacetResponse<O extends FacetOperation>(operation: O, raw: string | Uint8Array): FacetResponseMap[O] {
  requireFacet(isOperation(operation));
  requireFacet((typeof raw === "string" ? size(raw) : raw.length) <= facetLimits(operation).response_bytes, "response_too_large");
  return validateFacetDefinition(operation + ".response", strict(raw));
}
export function facetLimits(operation: FacetBridgeOperation): FacetLimits {
  requireFacet(operation === RESOLVE_OPERATION || isOperation(operation), "unsupported_capability");
  const page = operation.endsWith(".list") || operation === "facet.query.page";
  const read = page || /\.(get|state|preview)$/.test(operation) || operation === RESOLVE_OPERATION;
  return { request_bytes: operation === "review.propose_from_facet_query" ? 1048576 : 65536, response_bytes: page ? 250000 : read ? 1048576 : 4096 };
}

export interface ManagedRecordRequest { dto_version: 1; workspace_id: string; relative_path: string }
export interface ManagedRecordResponse { status: "ok"; dto_version: 1; workspace_id: string; record_id: string; revision_id: string; subject: { kind: "source" | "canonical"; id: string } }
export function parseManagedRecordRequest(value: unknown): ManagedRecordRequest {
  const v = exact(value, ["dto_version", "workspace_id", "relative_path"]);
  requireFacet(v.dto_version === 1 && identifier(v.workspace_id, ["workspace"]));
  requireFacet(typeof v.relative_path === "string" && size(v.relative_path) <= 4096 && !/[\ud800-\udfff]/u.test(v.relative_path) && !controls.test(v.relative_path) && !v.relative_path.includes("\\") && !v.relative_path.startsWith("/") && !/^[A-Za-z]:/.test(v.relative_path) && /\.md$/i.test(v.relative_path));
  requireFacet(v.relative_path.split("/").every((part: string) => part.length > 0 && part !== "." && part !== ".."));
  return { ...v } as ManagedRecordRequest;
}
export function parseManagedRecordResponse(value: unknown): ManagedRecordResponse {
  const v = exact(value, ["status", "dto_version", "workspace_id", "record_id", "revision_id", "subject"]);
  const subject = exact(v.subject, ["kind", "id"]);
  requireFacet(v.status === "ok" && v.dto_version === 1 && identifier(v.workspace_id, ["workspace"]) && ["source", "canonical"].includes(subject.kind));
  const source = subject.kind === "source";
  requireFacet(identifier(subject.id, [source ? "source" : "page"]) && identifier(v.record_id, [source ? "capture" : "page"]) && identifier(v.revision_id, [source ? "capture" : "revision"]));
  requireFacet(source ? v.record_id === v.revision_id : v.record_id === subject.id);
  return { ...v, subject: { ...subject } } as ManagedRecordResponse;
}

export type FacetNegotiation = { status: "available"; operations: FacetBridgeOperation[]; limits: Partial<Record<FacetBridgeOperation, FacetLimits>> } | { status: "unavailable"; reason: "upgrade_required" | "incompatible" };
export function negotiateFacets(raw: unknown): FacetNegotiation {
  try {
    const handshake = parseHandshake(raw);
    const input = object(raw);
    if (!own(input, "facet_capabilities")) return { status: "unavailable", reason: "upgrade_required" };
    const descriptor = exact(input.facet_capabilities, ["version", "operations"]);
    requireFacet(descriptor.version === 1 && Array.isArray(descriptor.operations) && descriptor.operations.length <= 128);
    const seen = new Set<string>();
    const operations: FacetBridgeOperation[] = [];
    const limits: Partial<Record<FacetBridgeOperation, FacetLimits>> = {};
    for (const rawEntry of descriptor.operations) {
      const entry = exact(rawEntry, ["operation", "dto_versions", "request_bytes", "response_bytes"]);
      requireFacet(typeof entry.operation === "string" && /^[a-z][a-z0-9_.]{0,127}$/.test(entry.operation) && !seen.has(entry.operation));
      seen.add(entry.operation);
      requireFacet(Array.isArray(entry.dto_versions) && entry.dto_versions.length > 0 && entry.dto_versions.length <= 16 && entry.dto_versions.every((v: unknown) => safeInteger(v, 1)) && new Set(entry.dto_versions).size === entry.dto_versions.length);
      requireFacet(safeInteger(entry.request_bytes, 1) && safeInteger(entry.response_bytes, 1));
      if (!isOperation(entry.operation) && entry.operation !== RESOLVE_OPERATION) continue;
      const operation = entry.operation as FacetBridgeOperation;
      const cap = facetLimits(operation);
      requireFacet(entry.request_bytes <= cap.request_bytes && entry.response_bytes <= cap.response_bytes);
      if (entry.dto_versions.includes(1) && handshake.operations.includes(operation)) {
        operations.push(operation); limits[operation] = { request_bytes: entry.request_bytes, response_bytes: entry.response_bytes };
      }
    }
    return operations.length ? { status: "available", operations, limits } : { status: "unavailable", reason: "incompatible" };
  } catch { return { status: "unavailable", reason: "incompatible" }; }
}

const protocol = { protocol: "open-brain-client", protocol_version: 1 } as const;
function requestId(value: unknown): void { requireFacet(identifier(value, ["plugin"], true)); }
export function encodeFacetRequest(operation: FacetBridgeOperation, args: unknown, id: string): Uint8Array {
  requestId(id);
  const arguments_ = operation === RESOLVE_OPERATION ? parseManagedRecordRequest(args) : parseFacetRequest(operation, JSON.stringify(args));
  const frame = new TextEncoder().encode(JSON.stringify({ ...protocol, request_id: id, operation, arguments: arguments_ }) + "\n");
  requireFacet(frame.length <= facetLimits(operation).request_bytes);
  return frame;
}
const errorCodes = new Set([...schema.definitions.error.properties.code.enum, "invalid_request", "incompatible_protocol", "unknown_operation", "protocol_error", "operation_failed"]);
export function parseFacetResponseFrame<O extends FacetBridgeOperation>(operation: O, raw: string | Uint8Array, id: string): O extends FacetOperation ? FacetResponseMap[O] : ManagedRecordResponse {
  requestId(id);
  const bytes = typeof raw === "string" ? new TextEncoder().encode(raw) : raw;
  requireFacet(bytes.length <= facetLimits(operation).response_bytes, "response_too_large");
  requireFacet(bytes[bytes.length - 1] === 10 && !bytes.subarray(0, -1).includes(10), "protocol_error");
  const v = object(strict(bytes));
  requireFacet(v.protocol === protocol.protocol && v.protocol_version === 1 && v.request_id === id, "protocol_error");
  if (v.ok === false) {
    exact(v, ["protocol", "protocol_version", "request_id", "ok", "error"]);
    const error = exact(v.error, ["code"]);
    requireFacet(errorCodes.has(error.code), "protocol_error");
    throw new FacetWireError(error.code);
  }
  exact(v, ["protocol", "protocol_version", "request_id", "ok", "result"]);
  requireFacet(v.ok === true, "protocol_error");
  return (operation === RESOLVE_OPERATION ? parseManagedRecordResponse(v.result) : parseFacetResponse(operation as FacetOperation, JSON.stringify(v.result))) as O extends FacetOperation ? FacetResponseMap[O] : ManagedRecordResponse;
}
