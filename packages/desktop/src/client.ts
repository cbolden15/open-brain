import { invoke } from "@tauri-apps/api/core";

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
  payload_family: "text" | "document" | "media" | "reference_or_file";
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
  let summary: RecordSummary | null = null;
  let text = "";
  const seen = new Set<string>();
  for (let chunks = 0; chunks < 512; chunks += 1) {
    if (signal?.aborted) throw "cancelled";
    const page: RecordReadPage = await request<RecordReadPage>("record.read", {
      dto_version: 1,
      record_id: record.record_id,
      expected_revision_id: record.revision_id,
      target_bytes: 32768,
      cursor,
    });
    if (signal?.aborted) throw "cancelled";
    if (page.dto_version !== 1 || page.content.kind !== "untrusted_text" ||
      page.record.record_id !== record.record_id || page.record.revision_id !== record.revision_id ||
      page.start_byte !== expectedStart || page.end_byte < page.start_byte ||
      page.end_byte - page.start_byte !== new TextEncoder().encode(page.content.text).length ||
      page.complete !== (page.next_cursor === null)) throw "malformed_response";
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
