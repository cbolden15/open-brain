import { invoke } from "@tauri-apps/api/core";

export interface BrainStatus {
  status: "ok";
  brain_root: string;
  initialized: boolean;
  state_schema_version: number;
  runtime_session_version: number;
}
export interface SearchHit {
  result_id: string;
  title: string;
  excerpt: string;
  record_type: string;
  source_origin: string;
  trust: string;
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
  invalid_arguments: "Check the selected options and absolute project path, then try again.",
  invalid_request: "Check the input size and selected options, then try again.",
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
