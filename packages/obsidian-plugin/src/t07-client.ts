import { randomUUID } from "node:crypto";

import { BridgeError, type OpenBrainBridge } from "./bridge";
import { record, safeRelativePath, type Handshake } from "./contracts";
import { validateT03Wire } from "./t03-wire";

type Bridge = Pick<OpenBrainBridge, "invoke">;

export interface InboxItem {
  capture_id: string;
  payload_family: string;
  preview: string;
  space_id: string | null;
  title: string | null;
}

export interface SpaceItem { name: string; slug: string; space_id: string }

export interface RecordSummary {
  excerpt: string;
  payload_family: string;
  provenance: {
    representative_capture_id: string;
    capture_ids: string[];
    source_origin: "owner_authored" | "third_party" | "mixed" | "unknown";
  };
  record_id: string;
  record_type: "source" | "canonical";
  revision_id: string;
  source_id: string | null;
  source_update_available: boolean;
  space_id: string | null;
  title: string;
  trust: string;
}

export interface SearchPage {
  complete: boolean;
  mode_used: string;
  next_cursor: string | null;
  results: RecordSummary[];
  warnings: string[];
}

export interface SearchRequest {
  cursor: string | null;
  filters: { payload_families: string[]; record_types: string[]; space_ids: string[] };
  limit: number;
  mode: "lexical" | "hybrid_preferred" | "hybrid_required";
  query: string;
}

export interface PublicationInspection {
  capture_ids: string[];
  evidence: { capture_id: string; excerpt: string; projection_applied: boolean; sha256: string }[];
  markdown: string;
  page_id: string;
  proposal_id: string;
  proposal_status: "pending" | "approved" | "rejected" | "edited";
  review_token: string;
  space_id: string;
  title: string;
}

export interface PublicationResult { page_id: string; publication_id: string; proposal_id: string }

export interface WorkspaceNote { note_id: string; relative_path: string; revision_id: string }
export interface PublicationWorkspace { ready: boolean; vault_path: string }

const PUBLICATION_OPERATIONS = [
  "inbox.list", "inbox.route", "publication.approve", "publication.edit_and_approve",
  "publication.propose", "publication.reject", "publication.show", "space.create",
  "space.list", "workspace.refresh", "workspace.setup", "workspace.status",
] as const;

export async function clientCapabilities(bridge: Bridge, handshake: Handshake): Promise<Set<string>> {
  const advertised = new Set(handshake.operations);
  const supported = new Set<string>();
  for (const operation of PUBLICATION_OPERATIONS) if (advertised.has(operation)) supported.add(operation);
  if (!advertised.has("contract.describe")) return supported;
  const value = validateT03Wire("contract.describe.response", await bridge.invoke("contract.describe", {}));
  for (const entry of value.operations as Record<string, unknown>[]) {
    const operation = entry;
    supported.add(operation.name as string);
  }
  return supported;
}

export function requireCapabilities(capabilities: Set<string>, operations: readonly string[]): void {
  if (operations.some((operation) => !capabilities.has(operation))) {
    throw new BridgeError("unsupported_capability");
  }
}

export async function preparePublicationWorkspace(
  bridge: Bridge,
  sameVault: (path: string) => Promise<boolean>,
): Promise<PublicationWorkspace> {
  let value = record(await bridge.invoke("workspace.status", {}));
  if (value.status === "unconfigured") {
    value = record(await bridge.invoke("workspace.setup", {}, 30_000));
    if (value.status !== "setup" || !text(value.vault_path)) throw new BridgeError("protocol_error");
  } else if (value.status !== "ok" || !text(value.vault_path)) {
    throw new BridgeError("protocol_error");
  }
  return { ready: await sameVault(value.vault_path), vault_path: value.vault_path };
}

export async function listInbox(bridge: Bridge): Promise<InboxItem[]> {
  const items: InboxItem[] = [];
  const seen = new Set<string>();
  let offset: number | null = 0;
  while (offset !== null) {
    const value = record(await bridge.invoke("inbox.list", { dto_version: 1, limit: 50, offset, unassigned_only: false }));
    if (value.status !== "listed" || !Array.isArray(value.items)) throw new BridgeError("protocol_error");
    for (const raw of value.items) {
      const item = record(raw);
      if (!text(item.capture_id) || !text(item.payload_family) || !displayText(item.preview) ||
          (item.space_id !== null && !text(item.space_id)) || (item.title !== null && !displayText(item.title))) {
        throw new BridgeError("protocol_error");
      }
      if (seen.has(item.capture_id)) throw new BridgeError("protocol_error");
      seen.add(item.capture_id);
      items.push(item as unknown as InboxItem);
    }
    offset = nextOffset(value.next_offset, offset);
    if (items.length > 2_000) throw new BridgeError("response_too_large");
  }
  return items;
}

export async function listSpaces(bridge: Bridge): Promise<SpaceItem[]> {
  const spaces: SpaceItem[] = [];
  const seen = new Set<string>();
  let offset: number | null = 0;
  while (offset !== null) {
    const value = record(await bridge.invoke("space.list", { dto_version: 1, limit: 50, offset }));
    if (value.status !== "listed" || !Array.isArray(value.spaces)) throw new BridgeError("protocol_error");
    for (const raw of value.spaces) {
      const space = record(raw);
      if (!text(space.space_id) || !displayText(space.name) || !text(space.slug)) throw new BridgeError("protocol_error");
      if (seen.has(space.space_id)) throw new BridgeError("protocol_error");
      seen.add(space.space_id);
      spaces.push(space as unknown as SpaceItem);
    }
    offset = nextOffset(value.next_offset, offset);
    if (spaces.length > 2_000) throw new BridgeError("response_too_large");
  }
  return spaces;
}

export async function createSpace(bridge: Bridge, name: string): Promise<SpaceItem> {
  const value = record(await bridge.invoke("space.create", {
    dto_version: 1, idempotency_key: operationKey("space"), name,
  }, 30_000));
  const space = record(value.space);
  if (value.status !== "created" || !text(space.space_id) || !displayText(space.name) || !text(space.slug)) {
    throw new BridgeError("protocol_error");
  }
  return space as unknown as SpaceItem;
}

export async function routeCaptures(bridge: Bridge, items: InboxItem[], spaceId: string): Promise<void> {
  if (items.length < 1 || items.length > 32) throw new BridgeError("invalid_selection");
  if (items.some((item) => item.space_id !== null && item.space_id !== spaceId)) throw new BridgeError("mixed_source_spaces");
  for (const item of items) {
    if (item.space_id === spaceId) continue;
    const value = record(await bridge.invoke("inbox.route", {
      capture_id: item.capture_id, dto_version: 1,
      idempotency_key: operationKey(`route-${item.capture_id}`), space_id: spaceId,
    }, 30_000));
    if (value.status !== "routed" || value.capture_id !== item.capture_id || value.space_id !== spaceId) {
      throw new BridgeError("protocol_error");
    }
  }
}

export async function searchPage(bridge: Bridge, request: SearchRequest): Promise<SearchPage> {
  const wireRequest = { dto_version: 1, ...request };
  validateT03Wire("search.page.request", wireRequest);
  const value = validateT03Wire("search.page.response", await bridge.invoke("search.page", wireRequest, 30_000));
  const results = (value.results as Record<string, unknown>[]).map(parseRecord);
  return { complete: value.complete as boolean, mode_used: value.mode_used as string,
    next_cursor: value.next_cursor as string | null, results, warnings: value.warnings as string[] };
}

export async function readCompleteRecord(
  bridge: Bridge,
  recordId: string,
  revisionId: string,
  cancelled: () => boolean = () => false,
): Promise<string> {
  let cursor: string | null = null;
  let expectedStart = 0;
  let result = "";
  for (let calls = 0; calls < 500; calls += 1) {
    if (cancelled()) throw new BridgeError("cancelled");
    const wireRequest = {
      cursor, dto_version: 1, expected_revision_id: revisionId, record_id: recordId, target_bytes: 32768,
    };
    validateT03Wire("record.read.request", wireRequest);
    const value = validateT03Wire("record.read.response", await bridge.invoke("record.read", wireRequest, 30_000));
    if (cancelled()) throw new BridgeError("cancelled");
    const content = record(value.content);
    const returnedRecord = parseRecord(value.record);
    if (value.start_byte !== expectedStart) throw new BridgeError("protocol_error");
    if (returnedRecord.record_id !== recordId || returnedRecord.revision_id !== revisionId) throw new BridgeError("revision_changed");
    const textChunk = content.text as string;
    const bytes = new TextEncoder().encode(textChunk).length;
    if (Number(value.end_byte) - expectedStart !== bytes) throw new BridgeError("protocol_error");
    result += textChunk;
    expectedStart = Number(value.end_byte);
    if (value.complete === true) {
      if (value.next_cursor !== null) throw new BridgeError("protocol_error");
      return result;
    }
    if (!text(value.next_cursor)) throw new BridgeError("protocol_error");
    cursor = value.next_cursor;
  }
  throw new BridgeError("response_too_large");
}

export async function proposePublication(bridge: Bridge, items: InboxItem[], title: string, markdown: string): Promise<PublicationInspection> {
  if (items.length < 1 || items.length > 32) throw new BridgeError("invalid_selection");
  const proposed = record(await bridge.invoke("publication.propose", {
    capture_ids: items.map((item) => item.capture_id), dto_version: 1,
    idempotency_key: operationKey("propose"), markdown, title,
  }, 30_000));
  if (proposed.status !== "proposed" || !text(proposed.proposal_id)) throw new BridgeError("protocol_error");
  const inspection = await inspectPublication(bridge, proposed.proposal_id);
  const captureIds = items.map((item) => item.capture_id);
  if (inspection.capture_ids.length !== captureIds.length ||
      inspection.capture_ids.some((captureId, index) => captureId !== captureIds[index])) {
    throw new BridgeError("review_conflict");
  }
  return inspection;
}

export async function inspectPublication(bridge: Bridge, proposalId: string): Promise<PublicationInspection> {
  const value = record(await bridge.invoke("publication.show", { dto_version: 1, proposal_id: proposalId }, 30_000));
  if (value.status !== "shown" || !text(value.proposal_id) || !text(value.page_id) || !text(value.space_id) ||
      !displayText(value.title) || typeof value.markdown !== "string" || !digest(value.review_token) ||
      !Array.isArray(value.capture_ids) || !value.capture_ids.every(text) || !Array.isArray(value.evidence) ||
      !["pending", "approved", "rejected", "edited"].includes(String(value.proposal_status))) throw new BridgeError("protocol_error");
  const evidence = value.evidence.map((raw) => {
    const item = record(raw);
    if (!text(item.capture_id) || !displayText(item.excerpt) || !digest(item.sha256) || typeof item.projection_applied !== "boolean") {
      throw new BridgeError("protocol_error");
    }
    return item as unknown as PublicationInspection["evidence"][number];
  });
  return { capture_ids: value.capture_ids as string[], evidence, markdown: value.markdown, page_id: value.page_id,
    proposal_id: value.proposal_id, proposal_status: value.proposal_status as PublicationInspection["proposal_status"],
    review_token: value.review_token, space_id: value.space_id, title: value.title };
}

export async function decidePublication(bridge: Bridge, inspection: PublicationInspection,
  decision: "approve" | "reject" | "edit_and_approve", editedMarkdown?: string): Promise<PublicationResult | null> {
  const args: Record<string, unknown> = { dto_version: 1, idempotency_key: operationKey(decision),
    proposal_id: inspection.proposal_id, review_token: inspection.review_token };
  if (decision === "edit_and_approve") args.markdown = editedMarkdown;
  const value = record(await bridge.invoke(`publication.${decision}`, args, 30_000));
  if (decision === "reject") {
    if (value.status !== "rejected" || value.proposal_id !== inspection.proposal_id) throw new BridgeError("protocol_error");
    return null;
  }
  if (![(decision === "approve" ? "approved" : "edited")].includes(String(value.status)) ||
      value.proposal_id !== inspection.proposal_id || !text(value.page_id) || !text(value.publication_id)) {
    throw new BridgeError("protocol_error");
  }
  return value as unknown as PublicationResult;
}

export async function refreshAndFindApprovedNote(bridge: Bridge, pageId: string): Promise<WorkspaceNote> {
  const value = record(await bridge.invoke("workspace.refresh", {}, 30_000));
  if (value.status !== "refreshed" || !Array.isArray(value.notes)) throw new BridgeError("protocol_error");
  const matches = value.notes.filter((raw) => record(raw).note_id === pageId);
  if (matches.length !== 1) throw new BridgeError("source_unavailable");
  const note = record(matches[0]);
  if (!text(note.revision_id) || !safeRelativePath(note.relative_path)) throw new BridgeError("invalid_source_path");
  return note as unknown as WorkspaceNote;
}

export function deterministicDraft(items: InboxItem[]): { markdown: string; title: string } {
  if (items.length < 1 || items.length > 32) throw new BridgeError("invalid_selection");
  const firstTitle = items.find((item) => item.title !== null)?.title?.trim();
  const title = firstTitle === undefined || firstTitle.length === 0 ? "Open Brain publication" : firstTitle;
  const markdown = items.map((item) => item.preview.trim()).filter(Boolean).join("\n\n---\n\n");
  return { markdown: markdown || "Captured material", title };
}

function parseRecord(raw: unknown): RecordSummary {
  const item = record(raw);
  const provenance = record(item.provenance);
  return {
    excerpt: item.excerpt as string,
    payload_family: item.payload_family as string,
    provenance: {
      representative_capture_id: provenance.representative_capture_id as string,
      capture_ids: [...(provenance.capture_ids as string[])],
      source_origin: provenance.source_origin as RecordSummary["provenance"]["source_origin"],
    },
    record_id: item.record_id as string,
    record_type: item.record_type as RecordSummary["record_type"],
    revision_id: item.revision_id as string,
    source_id: item.source_id as string | null,
    source_update_available: item.source_update_available as boolean,
    space_id: item.space_id as string | null,
    title: item.title as string,
    trust: item.trust as string,
  };
}

function nextOffset(value: unknown, previous: number): number | null {
  if (value === null) return null;
  if (!Number.isInteger(value) || Number(value) <= previous) throw new BridgeError("protocol_error");
  return Number(value);
}

function operationKey(prefix: string): string { return `obsidian-${prefix}-${randomUUID()}`.slice(0, 128); }
function text(value: unknown): value is string { return typeof value === "string" && value.length > 0 && !value.includes("\0"); }
function displayText(value: unknown): value is string { return text(value) && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/u.test(value); }
function digest(value: unknown): value is string { return typeof value === "string" && /^[0-9a-f]{64}$/.test(value); }
