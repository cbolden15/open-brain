export const PLUGIN_PROTOCOL = "open-brain-plugin-v1";

export type PluginOperation =
  | "brain.initialize"
  | "capture.create"
  | "graph.accept"
  | "graph.canvas"
  | "graph.refresh_semantic"
  | "graph.refresh_structural"
  | "graph.review"
  | "graph.suggestions"
  | "policy.exclusions"
  | "policy.set_exclusion"
  | "search.query"
  | "provider.configure"
  | "provider.remove"
  | "provider.status"
  | "system.handshake"
  | "workspace.conflict_review"
  | "workspace.conflicts"
  | "workspace.reconcile"
  | "workspace.refresh"
  | "workspace.resolve"
  | "workspace.setup"
  | "workspace.status";

export interface BridgeRequest {
  arguments: Record<string, unknown>;
  operation: PluginOperation;
  protocol: typeof PLUGIN_PROTOCOL;
  request_id: string;
}

export interface Handshake {
  desktop_only: true;
  operations: string[];
  product_version: string;
  protocol: typeof PLUGIN_PROTOCOL;
  status: "ok";
}

export interface WorkspaceStatus {
  active_notes: number;
  connected: boolean;
  inactive_notes: number;
  open_conflicts: number;
  pending_suggestions: number;
  status: "ok";
  vault_path: string;
  workspace_id: string;
}

export interface SearchItem {
  excerpt: string;
  relative_path?: string;
  result_id: string;
  title: string;
}

export interface SearchResponse {
  results: SearchItem[];
  status: "ok";
}

export interface SuggestionSummary {
  model: string;
  provider: string;
  revision_status: "current" | "stale";
  source_note_id: string;
  source_quote: string;
  source_revision_id: string;
  suggestion_id: string;
  target_note_id: string;
  target_quote: string;
  target_revision_id: string;
}

export interface SuggestionsResponse {
  status: "ok";
  suggestions: SuggestionSummary[];
  workspace_id: string;
}

export interface ReviewEndpoint {
  note_id: string;
  quote: string;
  relative_path: string;
  revision_id: string;
}

export interface SuggestionReview {
  append_text: string;
  model: string;
  provider: string;
  revision_status: "current" | "stale";
  source: ReviewEndpoint;
  status: "review";
  suggestion_id: string;
  target: ReviewEndpoint;
}

export interface CanvasResponse {
  canvas: {
    edges: Record<string, unknown>[];
    nodes: Record<string, unknown>[];
  };
  generation_id: string | null;
  snapshot_sha256: string | null;
  status: "fresh" | "missing" | "stale";
  workspace_id: string;
}

export interface ReconcileResponse {
  accepted_note_ids: string[];
  generation: number;
  missing_note_ids: string[];
  status: "reconciled";
  workspace_id: string;
}

export interface ConflictSummary {
  conflict_id: string;
  note_id: string;
  relative_path: string;
}

export interface ConflictsResponse {
  conflicts: ConflictSummary[];
  open_conflicts: number;
  status: "ok";
  workspace_id: string;
}

export interface ConflictReview {
  accepted_body: string;
  accepted_revision_id: string;
  conflict_id: string;
  note_id: string;
  relative_path: string;
  status: "review";
  workspace_body: string;
}

export interface Exclusion {
  kind: "folder" | "note";
  relative_path: string;
}

export interface ExclusionsResponse {
  exclusions: Exclusion[];
  status: "ok";
  workspace_id: string;
}

export type ProviderName =
  | "openai_api"
  | "anthropic_api"
  | "gemini_api"
  | "claude_subscription";

export interface ProviderOption {
  access_mode: "api_key" | "subscription";
  credential_saved: boolean | null;
  provider: ProviderName;
  reason?: string;
  status: "available" | "blocked";
}

export interface ProviderSelection {
  access_mode: "api_key";
  custody: "os" | "session";
  provider: Exclude<ProviderName, "claude_subscription">;
}

export interface ProviderStatus {
  os_store: "unavailable" | "macos_keychain" | "linux_secret_service";
  providers: ProviderOption[];
  selected: ProviderSelection | null;
  status: "ok";
}

export interface SemanticRefresh {
  actual_model?: string;
  reason?: string;
  remaining_attempts: number;
  remaining_input_bytes: number;
  status: "failed" | "refreshed";
}

export function parseHandshake(value: unknown): Handshake {
  const item = record(value);
  if (
    item.protocol !== PLUGIN_PROTOCOL ||
    item.status !== "ok" ||
    item.desktop_only !== true ||
    !strings(item.operations) ||
    !text(item.product_version)
  ) {
    throw new Error("invalid handshake");
  }
  return item as unknown as Handshake;
}

export function parseWorkspaceStatus(value: unknown): WorkspaceStatus | null {
  const item = record(value);
  if (item.status === "unconfigured") return null;
  if (
    item.status !== "ok" ||
    typeof item.connected !== "boolean" ||
    !nonnegative(item.active_notes) ||
    !nonnegative(item.inactive_notes) ||
    !nonnegative(item.open_conflicts) ||
    !nonnegative(item.pending_suggestions) ||
    !text(item.vault_path) ||
    !text(item.workspace_id)
  ) {
    throw new Error("invalid workspace status");
  }
  return item as unknown as WorkspaceStatus;
}

export function parseSearch(value: unknown): SearchResponse {
  const item = record(value);
  if (item.status !== "ok" || !Array.isArray(item.results) || item.results.length > 100) {
    throw new Error("invalid search response");
  }
  const results = item.results.map((entry) => {
    const result = record(entry);
    if (
      !text(result.excerpt) ||
      !text(result.result_id) ||
      !text(result.title) ||
      (result.relative_path !== undefined && !safeRelativePath(result.relative_path))
    ) {
      throw new Error("invalid search result");
    }
    return {
      excerpt: result.excerpt,
      relative_path: result.relative_path,
      result_id: result.result_id,
      title: result.title,
    } as SearchItem;
  });
  return { results, status: "ok" };
}

export function parseSuggestions(value: unknown): SuggestionsResponse {
  const item = record(value);
  if (
    item.status !== "ok" ||
    !text(item.workspace_id) ||
    !Array.isArray(item.suggestions) ||
    item.suggestions.length > 64
  ) {
    throw new Error("invalid suggestions response");
  }
  const suggestions = item.suggestions.map((entry) => {
    const suggestion = record(entry);
    for (const key of [
      "model",
      "provider",
      "source_note_id",
      "source_quote",
      "source_revision_id",
      "suggestion_id",
      "target_note_id",
      "target_quote",
      "target_revision_id",
    ]) {
      if (!text(suggestion[key])) throw new Error("invalid suggestion");
    }
    if (suggestion.revision_status !== "current" && suggestion.revision_status !== "stale") {
      throw new Error("invalid suggestion status");
    }
    return suggestion as unknown as SuggestionSummary;
  });
  return { status: "ok", suggestions, workspace_id: item.workspace_id };
}

export function parseReview(value: unknown): SuggestionReview {
  const item = record(value);
  if (
    item.status !== "review" ||
    !text(item.append_text) ||
    !text(item.model) ||
    !text(item.provider) ||
    !text(item.suggestion_id) ||
    (item.revision_status !== "current" && item.revision_status !== "stale")
  ) {
    throw new Error("invalid suggestion review");
  }
  return {
    append_text: item.append_text,
    model: item.model,
    provider: item.provider,
    revision_status: item.revision_status,
    source: parseEndpoint(item.source),
    status: "review",
    suggestion_id: item.suggestion_id,
    target: parseEndpoint(item.target),
  };
}

export function parseCanvas(value: unknown): CanvasResponse {
  const item = record(value);
  const canvas = record(item.canvas);
  if (
    (item.status !== "fresh" && item.status !== "missing" && item.status !== "stale") ||
    !text(item.workspace_id) ||
    (item.generation_id !== null && !text(item.generation_id)) ||
    (item.snapshot_sha256 !== null && !text(item.snapshot_sha256)) ||
    !Array.isArray(canvas.nodes) ||
    !Array.isArray(canvas.edges) ||
    canvas.nodes.length > 65 ||
    canvas.edges.length > 128 ||
    !canvas.nodes.every(isRecord) ||
    !canvas.edges.every(isRecord)
  ) {
    throw new Error("invalid Canvas response");
  }
  return item as unknown as CanvasResponse;
}

export function parseReconcile(value: unknown): ReconcileResponse {
  const item = record(value);
  if (
    item.status !== "reconciled" ||
    !text(item.workspace_id) ||
    !nonnegative(item.generation) ||
    !strings(item.accepted_note_ids) ||
    !strings(item.missing_note_ids)
  ) {
    throw new Error("invalid reconciliation response");
  }
  return item as unknown as ReconcileResponse;
}

export function parseConflicts(value: unknown): ConflictsResponse {
  const item = record(value);
  if (
    item.status !== "ok" ||
    !text(item.workspace_id) ||
    !nonnegative(item.open_conflicts) ||
    !Array.isArray(item.conflicts) ||
    item.conflicts.length > 64 ||
    item.open_conflicts < item.conflicts.length
  ) {
    throw new Error("invalid conflicts response");
  }
  const conflicts = item.conflicts.map((entry) => {
    const conflict = record(entry);
    if (
      !text(conflict.conflict_id) ||
      !text(conflict.note_id) ||
      !safeRelativePath(conflict.relative_path)
    ) {
      throw new Error("invalid conflict summary");
    }
    return conflict as unknown as ConflictSummary;
  });
  return {
    conflicts,
    open_conflicts: item.open_conflicts as number,
    status: "ok",
    workspace_id: item.workspace_id,
  };
}

export function parseConflictReview(value: unknown): ConflictReview {
  const item = record(value);
  if (
    item.status !== "review" ||
    !text(item.accepted_revision_id) ||
    !text(item.conflict_id) ||
    !text(item.note_id) ||
    !safeRelativePath(item.relative_path) ||
    !conflictBody(item.accepted_body) ||
    !conflictBody(item.workspace_body)
  ) {
    throw new Error("invalid conflict review");
  }
  return item as unknown as ConflictReview;
}

export function parseExclusions(value: unknown): ExclusionsResponse {
  const item = record(value);
  if (
    item.status !== "ok" ||
    !text(item.workspace_id) ||
    !Array.isArray(item.exclusions) ||
    item.exclusions.length > 64
  ) {
    throw new Error("invalid exclusions response");
  }
  const exclusions = item.exclusions.map((entry) => {
    const exclusion = record(entry);
    if (
      (exclusion.kind !== "folder" && exclusion.kind !== "note") ||
      !safeRelativePath(exclusion.relative_path)
    ) {
      throw new Error("invalid exclusion");
    }
    return exclusion as unknown as Exclusion;
  });
  return { exclusions, status: "ok", workspace_id: item.workspace_id };
}

export function parseProviderStatus(value: unknown): ProviderStatus {
  const item = record(value);
  if (
    item.status !== "ok" ||
    ![
      "unavailable",
      "macos_keychain",
      "linux_secret_service",
    ].includes(String(item.os_store)) ||
    !Array.isArray(item.providers) ||
    item.providers.length !== 4
  ) {
    throw new Error("invalid provider status");
  }
  const providers = item.providers.map((entry) => {
    const provider = record(entry);
    if (
      !providerName(provider.provider) ||
      (provider.access_mode !== "api_key" && provider.access_mode !== "subscription") ||
      (provider.credential_saved !== null &&
        typeof provider.credential_saved !== "boolean") ||
      (provider.status !== "available" && provider.status !== "blocked") ||
      (provider.reason !== undefined && !text(provider.reason))
    ) {
      throw new Error("invalid provider option");
    }
    return provider as unknown as ProviderOption;
  });
  let selected: ProviderSelection | null = null;
  if (item.selected !== null) {
    const selection = record(item.selected);
    if (
      selection.access_mode !== "api_key" ||
      (selection.custody !== "os" && selection.custody !== "session") ||
      !directProviderName(selection.provider)
    ) {
      throw new Error("invalid provider selection");
    }
    selected = selection as unknown as ProviderSelection;
  }
  return {
    os_store: item.os_store as ProviderStatus["os_store"],
    providers,
    selected,
    status: "ok",
  };
}

export function parseSemanticRefresh(value: unknown): SemanticRefresh {
  const item = record(value);
  if (
    (item.status !== "failed" && item.status !== "refreshed") ||
    !nonnegative(item.remaining_attempts) ||
    !nonnegative(item.remaining_input_bytes) ||
    (item.actual_model !== undefined && !text(item.actual_model)) ||
    (item.reason !== undefined && !text(item.reason)) ||
    (item.status === "refreshed" && !text(item.actual_model)) ||
    (item.status === "failed" && !text(item.reason))
  ) {
    throw new Error("invalid semantic refresh");
  }
  return item as unknown as SemanticRefresh;
}

export function record(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) throw new Error("invalid bridge result");
  return value;
}

export function safeRelativePath(value: unknown): value is string {
  if (!text(value) || value.includes("\\") || value.includes("\0") || value.startsWith("/")) {
    return false;
  }
  const parts = value.split("/");
  return parts.every((part) => part !== "" && part !== "." && part !== "..");
}

function parseEndpoint(value: unknown): ReviewEndpoint {
  const item = record(value);
  if (
    !text(item.note_id) ||
    !text(item.quote) ||
    !safeRelativePath(item.relative_path) ||
    !text(item.revision_id)
  ) {
    throw new Error("invalid review endpoint");
  }
  return item as unknown as ReviewEndpoint;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && !value.includes("\0");
}

function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.length <= 2_000 && value.every(text);
}

function providerName(value: unknown): value is ProviderName {
  return [
    "openai_api",
    "anthropic_api",
    "gemini_api",
    "claude_subscription",
  ].includes(String(value));
}

function directProviderName(
  value: unknown,
): value is Exclude<ProviderName, "claude_subscription"> {
  return ["openai_api", "anthropic_api", "gemini_api"].includes(String(value));
}

function conflictBody(value: unknown): value is string {
  return (
    typeof value === "string" &&
    new TextEncoder().encode(value).length <= 1024 * 1024 &&
    !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/u.test(value)
  );
}

function nonnegative(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}
