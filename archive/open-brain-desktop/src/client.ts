import { invoke } from "@tauri-apps/api/core";
import { parseRecordReadPage, validateT03Wire } from "./t03-wire";

export interface BrainStatus {
  status: "ok";
  brain_root: string;
  initialized: boolean;
  state_schema_version: number;
  runtime_session_version: number;
}
export interface RecordSummary {
  record_id: string;
  record_type: "source" | "canonical";
  revision_id: string;
  source_id: string | null;
  payload_family: "text" | "event" | "measurement" | "reference_or_file";
  space_id: string | null;
  title: string;
  excerpt: string;
  trust: string;
  provenance: {
    representative_capture_id: string;
    capture_ids: string[];
    source_origin: string;
  };
  source_update_available: boolean;
}
export interface LegacySearchHit {
  result_id: string;
  title: string;
  excerpt: string;
  record_type: string;
  source_origin: string;
  trust: string;
}
export interface ContractDescription {
  status: "ok";
  contract_version: "t03.v1";
  operations: { name: string; dto_version: 1; required_grants: string[] }[];
  limits: {
    request_bytes: number;
    response_bytes: number;
    content_calls: number;
    content_bytes: number;
    history_calls: number;
    history_bytes: number;
  };
}
export type SearchMode = "lexical" | "hybrid_preferred" | "hybrid_required";
export interface SearchFilters {
  space_ids: string[];
  payload_families: RecordSummary["payload_family"][];
  record_types: RecordSummary["record_type"][];
}
export interface SearchPage {
  status: "ok";
  dto_version: 1;
  results: RecordSummary[];
  next_cursor: string | null;
  complete: boolean;
  mode_used: "lexical" | "hybrid";
  warnings: ("model_unavailable" | "projection_stale")[];
}
export interface RecordReadPage {
  status: "ok";
  dto_version: 1;
  record: RecordSummary;
  content: { kind: "untrusted_text"; text: string };
  start_byte: number;
  end_byte: number;
  next_cursor: string | null;
  complete: boolean;
}
export interface InboxItem {
  capture_id: string;
  payload_family: string;
  state: string;
  space_id: string | null;
  intent: string | null;
  capture_why: string;
  title: string | null;
  preview: string;
}
export interface Space {
  space_id: string;
  name: string;
  slug: string;
}
export interface PublicationInspection {
  status: "shown";
  proposal_status: string;
  proposal_id: string;
  title: string;
  space_id: string;
  page_id: string;
  target_page_id: string | null;
  operation: string;
  capture_ids: string[];
  selected_capture_ids: string[];
  evidence: { capture_id: string; excerpt: string; sha256: string; projection_applied: boolean }[];
  review_token: string;
  markdown: string;
  expected_page_sha256: string | null;
  expected_publication_id: string | null;
  projection_applied: boolean;
}
export interface WorkspaceRefresh {
  status: "refreshed";
  vault_path: string;
  notes: { note_id: string; revision_id: string; relative_path: string }[];
}
export interface WorkspaceStatusResult {
  status: "ok";
  connected: boolean;
  active_notes: number;
  inactive_notes: number;
  observation_generation: number;
  open_conflicts: number;
  pending_suggestions: number;
  policy_generation: number;
  workspace_id: string;
  vault_path: string;
}
export interface SetupInput {
  client: "claude-code" | "codex";
  scope: "project" | "user";
  project_dir?: string | null;
  allow_capture: boolean;
  allow_search: boolean;
  action: "configure" | "remove";
}
export interface SetupPreview {
  status: "preview";
  preview_id: string;
  action: "configure" | "remove";
  client: string;
  scope: string;
  brain_root: string;
  runtime_path: string;
  permissions: { capture: boolean; search: boolean };
  changes: { path: string; kind: string; operation: string; content: string }[];
  notices: string[];
}
export interface SetupResult {
  status: "configured" | "removed" | "unchanged";
  client: string;
  scope: string;
  brain_root: string;
  runtime_path: string;
  changes: string[];
  notices: string[];
}
export interface CollectorSourceStatus {
  source_id: string;
  status: "enabled" | "paused" | "disabled";
  outcome: string;
  interval_seconds: number;
  next_run_epoch: number | null;
  last_run_epoch: number | null;
  last_success_epoch: number | null;
  pause_ack_epoch: number | null;
  captured_count: number;
  failed_count: number;
}
export interface CollectorEnableInput {
  source_id: string;
  connector_name: string;
  connection_id: string;
  credential_ref: string;
  resource_id: string;
  resource_type: string;
  interval_seconds: number;
}
export interface CollectorStatus {
  status: "ready" | "not_configured";
  brain_root: string;
  sources: CollectorSourceStatus[];
}
export function request<T>(operation: string, arguments_: object = {}, requestId?: string): Promise<T> {
  return invoke<T>("desktop_request", { operation, arguments: arguments_, requestId: requestId ?? null });
}
const messages: Record<string, string> = {
  runtime_unavailable: "The bundled runtime is missing or cannot start. Rebuild or reinstall Open Brain Desktop.",
  runtime_digest_mismatch: "The bundled runtime has changed. Rebuild or reinstall the complete app.",
  component_manifest_invalid: "The app and runtime do not match. Install a complete matching desktop bundle.",
  incompatible_runtime: "This runtime does not support shared desktop and agent use. Rebuild or update the app.",
  incompatible_schema: "This Brain needs a compatible Open Brain runtime version.",
  unsupported_platform: "Use the matching build for macOS Apple silicon or Linux x86_64.",
  deadline_exceeded: "The local runtime took too long to respond. Retry or reconnect in Settings.",
  lost_response: "The runtime closed before confirming the operation. Retry to check the same save.",
  transport_failed: "The local runtime connection stopped. Retry or reconnect in Settings.",
  bridge_closed: "The runtime connection is closed. Retry or reconnect in Settings.",
  session_exhausted: "This session reached its limit. Retry to open a fresh runtime session.",
  database_busy: "Another operation is using this Brain. Wait a moment and retry.",
  operation_in_progress: "Another desktop operation is running. Wait for it to finish.",
  generated_instructions: "This instruction file is generated. Add memory guidance through its generator, or choose a project with editable instructions.",
  instructions_too_large: "This instruction file exceeds Codex’s default 32 KiB budget. Shorten it before applying setup.",
  setup_conflict: "An existing configuration or instruction conflicts with this setup. Resolve the conflict before trying again.",
  setup_preview_stale: "The configuration changed after this preview. Preview the changes again.",
  client_config_invalid: "The client configuration cannot be read safely. Correct its format before applying setup.",
  unsafe_config_path: "This configuration path cannot be updated safely. Choose a regular local project directory.",
  collector_unavailable: "The optional collector is not installed or is not available to this desktop build.",
  credential_missing: "Choose a connected account before enabling this source.",
  unsupported_collector_source: "This collector source is not supported by the current build.",
  unknown_collector_source: "This collector source is no longer configured. Refresh the collector status.",
  invalid_arguments: "Check the selected values and try again.",
  invalid_request: "Check the input size and selected options, then try again.",
  unsupported_capability: "This runtime does not provide that capability. Update Open Brain to use it.",
  cursor_stale: "Search results changed. Restart this search to continue from the beginning.",
  cursor_invalid: "This search continuation is no longer valid. Restart the search.",
  revision_changed: "This record changed while it was being read. Open it again for the current revision.",
  review_conflict: "The proposal changed after inspection. Inspect it again before deciding.",
  terminal_decision: "This proposal already has a final decision. Refresh the review list.",
  response_too_large: "The runtime response exceeded the safe desktop limit.",
  setup_required: "Set up the managed vault before publishing.",
  unsafe_vault_path: "Open Brain refused to open a note outside the managed vault.",
  note_not_approved: "Refresh the managed vault before opening this note.",
  cleanup_unconfirmed: "The local runtime did not stop cleanly. Close and reopen the desktop app.",
};
export function errorMessage(error: unknown): string {
  return messages[String(error)] ?? "Open Brain could not complete this operation. Retry or reconnect in Settings.";
}
export function saveMayHaveCompleted(error: unknown): boolean {
  return ["deadline_exceeded", "lost_response", "transport_failed", "bridge_closed", "malformed_response"].includes(String(error));
}
export function setupReady(input: SetupInput): boolean {
  return (input.scope === "user" || Boolean(input.project_dir?.trim().startsWith("/"))) &&
    (input.action === "remove" || input.allow_capture || input.allow_search);
}

export function parseWorkspaceStatus(value: unknown): WorkspaceStatusResult | null {
  const item = exactRecord(value, ["status"], ["connected", "active_notes", "inactive_notes", "observation_generation", "open_conflicts", "pending_suggestions", "policy_generation", "workspace_id", "vault_path"]);
  if (item.status === "unconfigured" && Object.keys(item).length === 1) return null;
  if (item.status !== "ok" || typeof item.connected !== "boolean" ||
    !["active_notes", "inactive_notes", "observation_generation", "open_conflicts", "pending_suggestions", "policy_generation"].every(key => nonnegative(item[key])) ||
    !wireText(item.workspace_id) || !absolutePath(item.vault_path)) throw "malformed_response";
  return item as unknown as WorkspaceStatusResult;
}

export function parseWorkspaceSetup(value: unknown): void {
  const item = exactRecord(value, ["status", "duplicate", "generation", "note_id", "workspace_id", "vault_path"]);
  if (item.status !== "setup" || typeof item.duplicate !== "boolean" ||
    (item.generation !== null && !nonnegative(item.generation)) ||
    (item.note_id !== null && !wireText(item.note_id)) || !wireText(item.workspace_id) || !absolutePath(item.vault_path)) throw "malformed_response";
}

export function parseInboxPage(value: unknown): { items: InboxItem[]; next_offset: number | null } {
  const page = exactRecord(value, ["status", "items", "offset", "next_offset"], ["offset_limit_reached"]);
  if (page.status !== "listed" || !nonnegative(page.offset) || !nullableOffset(page.next_offset) ||
    (page.offset_limit_reached !== undefined && typeof page.offset_limit_reached !== "boolean") ||
    !Array.isArray(page.items) || page.items.length > 100) throw "malformed_response";
  const items = page.items.map(raw => {
    const item = exactRecord(raw, ["capture_id", "payload_family", "state", "space_id", "intent", "capture_why", "title", "preview"]);
    if (!identifier(item.capture_id, ["capture"]) || !wireText(item.payload_family) || !wireText(item.state) ||
      (item.space_id !== null && !identifier(item.space_id, ["space"])) ||
      (item.intent !== null && !displayText(item.intent)) || !displayText(item.capture_why) ||
      (item.title !== null && !displayText(item.title)) || !displayText(item.preview, true)) throw "malformed_response";
    return item as unknown as InboxItem;
  });
  return { items, next_offset: page.next_offset as number | null };
}

export function parseSpacePage(value: unknown): { spaces: Space[] } {
  const page = exactRecord(value, ["status", "spaces", "offset", "next_offset"], ["offset_limit_reached"]);
  if (page.status !== "listed" || !nonnegative(page.offset) || !nullableOffset(page.next_offset) ||
    (page.offset_limit_reached !== undefined && typeof page.offset_limit_reached !== "boolean") ||
    !Array.isArray(page.spaces) || page.spaces.length > 100) throw "malformed_response";
  return { spaces: page.spaces.map(parseSpace) };
}

export function parseCreatedSpace(value: unknown): Space {
  const result = exactRecord(value, ["status", "space"]);
  if (result.status !== "created") throw "malformed_response";
  return parseSpace(result.space);
}

export function parseRoutedCapture(value: unknown, captureId: string, spaceId: string): void {
  const item = exactRecord(value, ["status", "capture_id", "space_id"]);
  if (item.status !== "routed" || item.capture_id !== captureId || item.space_id !== spaceId) throw "malformed_response";
}

export function parseProposal(value: unknown): { proposal_id: string; page_id: string } {
  const item = exactRecord(value, ["status", "proposal_status", "proposal_id", "page_id", "space_id", "target_page_id", "operation", "effective_idempotency_key"]);
  if (item.status !== "proposed" || item.proposal_status !== "pending" || !identifier(item.proposal_id, ["proposal"]) ||
    !identifier(item.page_id, ["page"]) || !identifier(item.space_id, ["space"]) ||
    (item.target_page_id !== null && !identifier(item.target_page_id, ["page"])) || !["create", "update"].includes(String(item.operation)) || !wireText(item.effective_idempotency_key)) throw "malformed_response";
  return { proposal_id: item.proposal_id as string, page_id: item.page_id as string };
}

export function parsePublicationInspection(value: unknown): PublicationInspection {
  const item = exactRecord(value, ["status", "proposal_status", "proposal_id", "title", "space_id", "page_id", "target_page_id", "operation", "capture_ids", "selected_capture_ids", "evidence", "review_token", "markdown", "expected_page_sha256", "expected_publication_id", "projection_applied"]);
  if (item.status !== "shown" || !["pending", "approved", "rejected", "edited"].includes(String(item.proposal_status)) ||
    !identifier(item.proposal_id, ["proposal"]) || !displayText(item.title) || !identifier(item.space_id, ["space"]) ||
    !identifier(item.page_id, ["page"]) || (item.target_page_id !== null && !identifier(item.target_page_id, ["page"])) ||
    !["create", "update"].includes(String(item.operation)) || !idArray(item.capture_ids, "capture", 32) || !idArray(item.selected_capture_ids, "capture", 32) ||
    !Array.isArray(item.evidence) || item.evidence.length > 32 || !digest(item.review_token) || typeof item.markdown !== "string" ||
    (item.expected_page_sha256 !== null && !digest(item.expected_page_sha256)) ||
    (item.expected_publication_id !== null && !identifier(item.expected_publication_id, ["publication"])) || typeof item.projection_applied !== "boolean") throw "malformed_response";
  const evidence = item.evidence.map(raw => {
    const row = exactRecord(raw, ["capture_id", "excerpt", "sha256", "projection_applied"]);
    if (!identifier(row.capture_id, ["capture"]) || !displayText(row.excerpt, true) || !digest(row.sha256) || typeof row.projection_applied !== "boolean") throw "malformed_response";
    return row as unknown as PublicationInspection["evidence"][number];
  });
  return { ...item, capture_ids: [...item.capture_ids as string[]], selected_capture_ids: [...item.selected_capture_ids as string[]], evidence } as unknown as PublicationInspection;
}

export function parseDecision(value: unknown, proposalId: string): { status: string; outcome: string; proposal_id: string; page_id: string; publication_id?: string } {
  const item = exactRecord(value, ["status", "outcome", "decision_id", "proposal_id", "page_id", "publication_id", "duplicate", "effective_idempotency_key"]);
  if (!["approved", "edited", "rejected"].includes(String(item.status)) || item.outcome !== item.status ||
    !identifier(item.decision_id, ["decision"]) || item.proposal_id !== proposalId || !identifier(item.page_id, ["page"]) ||
    (item.publication_id !== null && !identifier(item.publication_id, ["publication"])) || typeof item.duplicate !== "boolean" || !wireText(item.effective_idempotency_key)) throw "malformed_response";
  return item as unknown as { status: string; outcome: string; proposal_id: string; page_id: string; publication_id?: string };
}

export function parseWorkspaceRefresh(value: unknown): WorkspaceRefresh {
  const item = exactRecord(value, ["status", "duplicate", "generation", "note_id", "workspace_id", "vault_path", "notes"]);
  if (item.status !== "refreshed" || typeof item.duplicate !== "boolean" || !nonnegative(item.generation) ||
    (item.note_id !== null && !identifier(item.note_id, ["page"])) || !wireText(item.workspace_id) || !absolutePath(item.vault_path) || !Array.isArray(item.notes)) throw "malformed_response";
  const notes = item.notes.map(raw => {
    const note = exactRecord(raw, ["note_id", "revision_id", "relative_path"]);
    if (!identifier(note.note_id, ["page"]) || !identifier(note.revision_id, ["revision"]) || !safeRelativePath(note.relative_path)) throw "malformed_response";
    return note as unknown as WorkspaceRefresh["notes"][number];
  });
  return { status: "refreshed", vault_path: item.vault_path as string, notes };
}

export function negotiatedOperations(description: ContractDescription | null): Set<string> {
  if (!description || description.status !== "ok" || description.contract_version !== "t03.v1" || !Array.isArray(description.operations)) return new Set();
  const grants: Record<string, string> = {
    "search.page": "search",
    "record.read": "content-read",
    "history.list": "history-read",
    "history.show": "history-read",
    "source.route": "organize",
  };
  const accepted = new Set<string>();
  for (const item of description.operations) {
    if (!item || typeof item.name !== "string" || item.dto_version !== 1 ||
      !Array.isArray(item.required_grants) || item.required_grants.length !== 1 ||
      item.required_grants[0] !== grants[item.name]) continue;
    accepted.add(item.name);
  }
  return accepted;
}

export async function readCompleteRecord(
  record: Pick<RecordSummary, "record_id" | "revision_id">,
  signal?: AbortSignal,
): Promise<{ record: RecordSummary; text: string }> {
  let cursor: string | null = null;
  let expectedStart = 0;
  let outputBytes = 0;
  let summary: RecordSummary | null = null;
  let text = "";
  const seen = new Set<string>();
  for (let chunks = 0; chunks < 500; chunks += 1) {
    if (signal?.aborted) throw "cancelled";
    const wireRequest = {
      dto_version: 1,
      record_id: record.record_id,
      expected_revision_id: record.revision_id,
      target_bytes: 32768,
      cursor,
    };
    validateT03Wire("record.read.request", wireRequest);
    const rawPage = await request<unknown>("record.read", wireRequest);
    const page = parseRecordReadPage(rawPage);
    // Reserve more than the fixed bridge envelope, including its request ID.
    const encodedBytes = new TextEncoder().encode(JSON.stringify(rawPage)).length + 256;
    outputBytes += encodedBytes;
    if (encodedBytes > 1024 * 1024 || outputBytes > 16 * 1024 * 1024) throw "response_too_large";
    if (signal?.aborted) throw "cancelled";
    if (page.record.record_id !== record.record_id || page.record.revision_id !== record.revision_id) throw "revision_changed";
    if (page.start_byte !== expectedStart) throw "malformed_response";
    summary ??= page.record;
    text += page.content.text;
    expectedStart = page.end_byte;
    if (page.complete) return { record: summary ?? page.record, text };
    if (!page.next_cursor || seen.has(page.next_cursor)) throw "malformed_response";
    seen.add(page.next_cursor);
    cursor = page.next_cursor;
  }
  throw "response_too_large";
}

function exactRecord(value: unknown, required: string[], optional: string[] = []): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw "malformed_response";
  const item = value as Record<string, unknown>;
  const allowed = new Set([...required, ...optional]);
  if (!required.every(key => Object.prototype.hasOwnProperty.call(item, key)) || Object.keys(item).some(key => !allowed.has(key))) throw "malformed_response";
  return item;
}

function wireText(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && !value.includes("\0") && [...value].every(character => {
    const point = character.codePointAt(0)!;
    return point < 0xd800 || point > 0xdfff;
  });
}

function displayText(value: unknown, allowEmpty = false): value is string {
  return typeof value === "string" && (allowEmpty || value.length > 0) && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/u.test(value);
}

function identifier(value: unknown, prefixes: string[]): value is string {
  return typeof value === "string" && new RegExp(`^(?:${prefixes.join("|")})_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`).test(value);
}

function digest(value: unknown): value is string { return typeof value === "string" && /^[0-9a-f]{64}$/.test(value); }
function nonnegative(value: unknown): value is number { return Number.isSafeInteger(value) && Number(value) >= 0; }
function nullableOffset(value: unknown): value is number | null { return value === null || nonnegative(value); }
function absolutePath(value: unknown): value is string { return wireText(value) && value.startsWith("/"); }
function safeRelativePath(value: unknown): value is string {
  return wireText(value) && !value.includes("\\") && !value.startsWith("/") && value.split("/").every(part => part !== "" && part !== "." && part !== "..");
}
function idArray(value: unknown, prefix: string, maximum: number): value is string[] {
  return Array.isArray(value) && value.length >= 1 && value.length <= maximum && value.every(item => identifier(item, [prefix])) && new Set(value).size === value.length;
}

function parseSpace(value: unknown): Space {
  const item = exactRecord(value, ["space_id", "name", "slug"]);
  if (!identifier(item.space_id, ["space"]) || !displayText(item.name) || !wireText(item.slug)) throw "malformed_response";
  return item as unknown as Space;
}
