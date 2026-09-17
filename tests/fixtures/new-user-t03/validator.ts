// Executable fixture grammar; task adapters are implemented after contract freeze.
import { readFileSync } from "node:fs";
const contract = JSON.parse(readFileSync(new URL("./strict-contract.schema.json", import.meta.url), "utf8"));
type Rule = { ref?: string; anyOf?: Rule[]; enum?: unknown[]; type?: string; properties?: Record<string, Rule>; optional?: string[]; items?: Rule; min?: number; max?: number; unique?: boolean; id_prefixes?: string[]; format?: string };
const definitions = contract.definitions as Record<string, Rule>;
function require(condition: unknown): asserts condition { if (!condition) throw new Error("invalid_dto"); }
function check(rule: Rule, value: any): void {
  if (rule.ref) return check(definitions[rule.ref]!, value);
  if (rule.anyOf) {
    for (const option of rule.anyOf) { try { check(option, value); return; } catch {} }
    throw new Error("invalid_dto");
  }
  if (rule.enum) { require(rule.enum.some(item => item === value)); return; }
  switch (rule.type) {
    case "null": require(value === null); break;
    case "boolean": require(typeof value === "boolean"); break;
    case "integer": require(Number.isSafeInteger(value) && value >= rule.min! && value <= rule.max!); break;
    case "string": {
      require(typeof value === "string");
      const scalars = [...value];
      require(scalars.every(c => c.codePointAt(0)! !== 0 && !(c.codePointAt(0)! >= 0xd800 && c.codePointAt(0)! <= 0xdfff)));
      require(scalars.length >= (rule.min ?? 0) && scalars.length <= (rule.max ?? 1048576));
      if (rule.id_prefixes) require(new RegExp(`^(?:${rule.id_prefixes.join("|")})_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`).test(value));
      if (rule.format === "query") require(/[\p{L}\p{N}]/u.test(value) && new TextEncoder().encode(value).length <= 4096);
      if (rule.format === "timestamp") require(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(value));
      break;
    }
    case "array":
      require(Array.isArray(value) && value.length >= rule.min! && value.length <= rule.max!);
      if (rule.unique) require(new Set(value.map(item => JSON.stringify(item))).size === value.length);
      value.forEach(item => check(rule.items!, item)); break;
    case "object":
      require(value !== null && typeof value === "object" && !Array.isArray(value));
      require(Object.keys(value).every(key => Object.prototype.hasOwnProperty.call(rule.properties!, key)));
      require(Object.keys(rule.properties!).every(key => rule.optional!.includes(key) || Object.prototype.hasOwnProperty.call(value, key)));
      Object.entries(value).forEach(([key, child]) => check(rule.properties![key]!, child));
      if ("complete" in value) require(value.complete === (value.next_cursor === null));
      if ("representative_capture_id" in value) require(value.representative_capture_id === value.capture_ids[0]);
      if ("record_type" in value) {
        if (value.record_type === "source") {
          require(value.record_id.startsWith("capture_") && value.record_id === value.revision_id && value.source_id !== null);
          require(value.provenance.capture_ids.length === 1 && value.provenance.capture_ids[0] === value.record_id);
        } else require(value.record_id.startsWith("page_") && value.revision_id.startsWith("revision_") && value.source_id === null);
      }
      if ("content" in value) {
        const length = new TextEncoder().encode(value.content.text).length;
        require(value.end_byte - value.start_byte === length && (value.complete || length > 0));
      }
      if ("required_grants" in value) require(value.required_grants.length === 1 && value.required_grants[0] === (contract.grants as Record<string, string>)[value.name]);
      if ("operations" in value) require(new Set(value.operations.map((item: any) => item.name)).size === value.operations.length);
      break;
    default: throw new Error("invalid_schema");
  }
}
export function validate(name: string, value: unknown): unknown { check(definitions[name]!, value); return value; }
