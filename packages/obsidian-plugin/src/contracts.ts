export const PLUGIN_PROTOCOL = "open-brain-plugin-v1";

export type PluginOperation =
  | "brain.initialize"
  | "capture.create"
  | "graph.accept"
  | "graph.canvas"
  | "graph.refresh_structural"
  | "graph.review"
  | "graph.suggestions"
  | "search.query"
  | "system.handshake"
  | "workspace.reconcile"
  | "workspace.refresh"
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

function nonnegative(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}
