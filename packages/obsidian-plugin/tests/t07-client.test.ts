import { describe, expect, it, vi } from "vitest";

import { BridgeError } from "../src/bridge";
import type { Handshake, PluginOperation } from "../src/contracts";
import {
  clientCapabilities,
  decidePublication,
  deterministicDraft,
  inspectPublication,
  listInbox,
  proposePublication,
  preparePublicationWorkspace,
  readCompleteRecord,
  refreshAndFindApprovedNote,
  routeCaptures,
  searchPage,
  type InboxItem,
} from "../src/t07-client";

function fake(responses: Partial<Record<PluginOperation, unknown | unknown[]>>) {
  const calls = new Map<string, number>();
  const invoke = vi.fn(async (operation: PluginOperation) => {
    const response = responses[operation];
    const index = calls.get(operation) ?? 0;
    calls.set(operation, index + 1);
    if (Array.isArray(response)) return response[index];
    if (response instanceof Error) throw response;
    return response;
  });
  return { invoke, bridge: { invoke } as never };
}

const uuid = (n: number) => `123e4567-e89b-42d3-a456-${n.toString(16).padStart(12, "0")}`;
const capture = (n: number, space_id: string | null = null): InboxItem => ({
  capture_id: `capture_${uuid(n)}`,
  payload_family: "text", preview: `Source ${n} <button onclick=evil()>`, space_id, title: null,
});
const summary = (n: number, empty = false) => {
  const captureId = capture(n).capture_id;
  return { record_id: captureId, record_type: "source", revision_id: captureId,
    source_id: `source_${uuid(1000 + n)}`, payload_family: "text", space_id: null,
    title: empty ? "" : `Title ${n}`, excerpt: empty ? "" : `Excerpt ${n}`, trust: "third_party",
    provenance: { representative_capture_id: captureId, capture_ids: [captureId], source_origin: "third_party" },
    source_update_available: false };
};
const limits = { request_bytes: 65536, response_bytes: 1048576, content_calls: 500,
  content_bytes: 16777216, history_calls: 500, history_bytes: 16777216 };

const handshake = (operations: string[]): Handshake => ({
  desktop_only: true, operations, product_version: "0.1.0", protocol: "open-brain-client",
  protocol_version: 1, status: "ok",
});

describe("T07 negotiated clients", () => {
  it("uses discovery and does not assume unavailable retrieval grants", async () => {
    const { bridge } = fake({
      "contract.describe": { status: "ok", contract_version: "t03.v1", operations: [
        { name: "search.page", dto_version: 1, required_grants: ["search"] },
      ], limits },
    });
    const available = await clientCapabilities(bridge, handshake(["contract.describe", "publication.show"]));
    expect([...available].sort()).toEqual(["publication.show", "search.page"]);
    expect(available.has("record.read")).toBe(false);
  });

  it("traverses at least 201 inbox entries without duplication", async () => {
    const pages = Array.from({ length: 5 }, (_, page) => ({
      status: "listed", offset: page * 50,
      next_offset: page === 4 ? null : (page + 1) * 50,
      items: Array.from({ length: page === 4 ? 1 : 50 }, (_, i) => capture(page * 50 + i)),
    }));
    const { bridge, invoke } = fake({ "inbox.list": pages });
    const result = await listInbox(bridge);
    expect(result).toHaveLength(201);
    expect(new Set(result.map((item) => item.capture_id))).toHaveLength(201);
    expect(invoke).toHaveBeenCalledTimes(5);
  });

  it("keeps a control-bearing untrusted preview across all inbox pages and rejects malformed DTOs", async () => {
    const hostilePreview = "\u001b]52;c;SYNTHETIC_CLIPBOARD\u0007 <script>synthetic()<[protected]> [run](command:synthetic)";
    const hostile: InboxItem = {
      capture_id: `capture_${uuid(500)}`, payload_family: "text", preview: hostilePreview,
      space_id: null, title: null,
    };
    const pages = Array.from({ length: 5 }, (_, page) => ({
      status: "listed", offset: page * 50, next_offset: page === 4 ? null : (page + 1) * 50,
      items: [capture(page * 2), capture(page * 2 + 1)],
    }));
    pages[4]!.items.splice(1, 0, hostile);
    const { bridge, invoke } = fake({ "inbox.list": pages });

    const result = await listInbox(bridge);

    expect(result).toHaveLength(11);
    expect(result[9]?.capture_id).toBe(hostile.capture_id);
    expect(result[9]?.preview).toBe(hostilePreview);
    expect(deterministicDraft([result[9]!]).markdown).toBe(hostilePreview);
    expect(invoke).toHaveBeenCalledTimes(5);

    const malformed = { ...capture(99), capture_id: 99 };
    await expect(listInbox(fake({
      "inbox.list": { status: "listed", offset: 0, next_offset: null, items: [malformed] },
    }).bridge)).rejects.toEqual(expect.objectContaining<Partial<BridgeError>>({ code: "protocol_error" }));
  });

  it("routes only unrouted captures and refuses mixed existing spaces", async () => {
    const a = "space_123e4567-e89b-42d3-a456-426614174500";
    const b = "space_123e4567-e89b-42d3-a456-426614174501";
    const { bridge, invoke } = fake({ "inbox.route": { status: "routed", capture_id: capture(1).capture_id, space_id: a } });
    await routeCaptures(bridge, [capture(0, a), capture(1)], a);
    expect(invoke).toHaveBeenCalledOnce();
    await expect(routeCaptures(bridge, [capture(0, a), capture(1, b)], a)).rejects.toEqual(
      expect.objectContaining<Partial<BridgeError>>({ code: "mixed_source_spaces" }),
    );
  });

  it("reconstructs exact long Unicode chunks and rejects a byte discontinuity", async () => {
    const record = summary(0);
    const { bridge } = fake({ "record.read": [
      { status: "ok", dto_version: 1, record, content: { kind: "untrusted_text", text: "🙂中" }, start_byte: 0, end_byte: 7, next_cursor: "cursor-2", complete: false },
      { status: "ok", dto_version: 1, record, content: { kind: "untrusted_text", text: "e\u0301" }, start_byte: 7, end_byte: 10, next_cursor: null, complete: true },
    ] });
    await expect(readCompleteRecord(bridge, record.record_id, record.revision_id)).resolves.toBe("🙂中e\u0301");
    const broken = fake({ "record.read": { status: "ok", dto_version: 1, record,
      content: { kind: "untrusted_text", text: "x" }, start_byte: 1, end_byte: 2, next_cursor: null, complete: true } });
    await expect(readCompleteRecord(broken.bridge, record.record_id, record.revision_id)).rejects.toThrow("protocol_error");
  });

  it("retains filters and cursor explicitly and surfaces stale cursors without restart", async () => {
    const request = { cursor: "cursor-1", filters: { payload_families: ["text"], record_types: ["source"],
      space_ids: ["space_123e4567-e89b-42d3-a456-426614174500"] }, limit: 50, mode: "lexical" as const, query: "needle" };
    const stale = new BridgeError("cursor_stale");
    const { bridge, invoke } = fake({ "search.page": stale });
    await expect(searchPage(bridge, request)).rejects.toBe(stale);
    expect(invoke).toHaveBeenCalledWith("search.page", { dto_version: 1, ...request }, 30_000);
    expect(invoke).toHaveBeenCalledTimes(1);
  });

  it("bounds reconstructed UTF-8 bytes before returning a complete oversized record", async () => {
    const record = summary(0);
    const pages = Array.from({ length: 263 }, (_, index) => ({
      status: "ok", dto_version: 1, record,
      content: { kind: "untrusted_text", text: "🙂".repeat(16000) },
      start_byte: index * 64000, end_byte: (index + 1) * 64000,
      next_cursor: index === 262 ? null : `cursor-${index + 1}`, complete: index === 262,
    }));
    const { bridge, invoke } = fake({ "record.read": pages });
    await expect(readCompleteRecord(bridge, record.record_id, record.revision_id))
      .rejects.toThrow("response_too_large");
    expect(invoke.mock.calls.length).toBeLessThanOrEqual(263);
  });

  it("rejects a response beyond the encoded envelope budget before requesting another", async () => {
    const record = summary(0);
    const { bridge, invoke } = fake({ "record.read": {
      status: "ok", dto_version: 1, record,
      content: { kind: "untrusted_text", text: "🙂".repeat(300000) },
      start_byte: 0, end_byte: 1200000, next_cursor: "cursor-2", complete: false,
    } });
    await expect(readCompleteRecord(bridge, record.record_id, record.revision_id))
      .rejects.toThrow("response_too_large");
    expect(invoke).toHaveBeenCalledOnce();
  });

  it("traverses 201 filtered search results with explicit continuation and no duplicate accumulation", async () => {
    const pages = Array.from({ length: 5 }, (_, page) => ({
      status: "ok", dto_version: 1,
      results: Array.from({ length: page === 4 ? 1 : 50 }, (_, i) => {
        return summary(page * 50 + i);
      }),
      next_cursor: page === 4 ? null : `cursor-${page + 1}`, complete: page === 4,
      mode_used: "lexical", warnings: [],
    }));
    const { bridge } = fake({ "search.page": pages });
    const base = { filters: { payload_families: ["text"], record_types: ["source"], space_ids: ["space_123e4567-e89b-42d3-a456-000000000500"] },
      limit: 50, mode: "lexical" as const, query: "synthetic" };
    const ids: string[] = [];
    let cursor: string | null = null;
    do {
      const page = await searchPage(bridge, { ...base, cursor });
      ids.push(...page.results.map((item) => item.record_id));
      cursor = page.next_cursor;
    } while (cursor !== null);
    expect(ids).toHaveLength(201);
    expect(new Set(ids)).toHaveLength(201);
  });

  it("accepts valid empty display strings and rejects unknown or null provenance fields", async () => {
    const valid = { status: "ok", dto_version: 1, results: [summary(0, true)], next_cursor: null,
      complete: true, mode_used: "lexical", warnings: [] };
    await expect(searchPage(fake({ "search.page": valid }).bridge, {
      cursor: null, filters: { payload_families: [], record_types: [], space_ids: [] },
      limit: 10, mode: "lexical", query: "valid",
    })).resolves.toMatchObject({ results: [{ title: "", excerpt: "" }] });
    const invalid = { ...valid, unexpected: true, results: [{ ...summary(0), provenance: null, extra: true }] };
    await expect(searchPage(fake({ "search.page": invalid }).bridge, {
      cursor: null, filters: { payload_families: [], record_types: [], space_ids: [] },
      limit: 10, mode: "lexical", query: "valid",
    })).rejects.toThrow("protocol_error");
  });

  it("hands off to the managed vault before publication operations when setup differs", async () => {
    const { bridge, invoke } = fake({
      "workspace.status": { status: "unconfigured" },
      "workspace.setup": { status: "setup", vault_path: "/synthetic/managed", duplicate: false,
        generation: null, note_id: null, workspace_id: "workspace_123e4567-e89b-42d3-a456-000000000700" },
    });
    await expect(preparePublicationWorkspace(bridge, async () => false)).resolves.toEqual({
      ready: false, vault_path: "/synthetic/managed",
    });
    expect(invoke.mock.calls.map(([operation]) => operation)).toEqual(["workspace.status", "workspace.setup"]);

    const same = fake({ "workspace.status": { status: "ok", vault_path: "/synthetic/managed" } });
    await expect(preparePublicationWorkspace(same.bridge, async (path) => path === "/synthetic/managed")).resolves.toEqual({
      ready: true, vault_path: "/synthetic/managed",
    });
  });

  it("requires inspect-before-decision and opens only the exact approved page note", async () => {
    const item = capture(0, "space_123e4567-e89b-42d3-a456-426614174500");
    const shown = { status: "shown", proposal_status: "pending", proposal_id: "proposal_123e4567-e89b-42d3-a456-426614174300",
      page_id: "page_123e4567-e89b-42d3-a456-426614174400", space_id: item.space_id, title: "Title",
      markdown: "Body", capture_ids: [item.capture_id], selected_capture_ids: [item.capture_id],
      evidence: [{ capture_id: item.capture_id, excerpt: "<img onerror=evil>", sha256: "a".repeat(64), projection_applied: false }],
      review_token: "d".repeat(64), target_page_id: null, operation: "create", expected_page_sha256: null,
      expected_publication_id: null, projection_applied: false };
    const { bridge, invoke } = fake({
      "publication.propose": { status: "proposed", proposal_id: shown.proposal_id, page_id: shown.page_id },
      "publication.show": shown,
      "publication.approve": { status: "approved", outcome: "approved", proposal_id: shown.proposal_id,
        page_id: shown.page_id, publication_id: "publication_123e4567-e89b-42d3-a456-426614174900" },
      "workspace.refresh": { status: "refreshed", notes: [
        { note_id: "page_123e4567-e89b-42d3-a456-426614174401", revision_id: "revision_other", relative_path: "Other.md" },
        { note_id: shown.page_id, revision_id: "revision_target", relative_path: "Approved.md" },
      ] },
    });
    const inspection = await proposePublication(bridge, [item], "Title", "Body");
    const approved = await decidePublication(bridge, inspection, "approve");
    expect(approved?.page_id).toBe(shown.page_id);
    await expect(refreshAndFindApprovedNote(bridge, shown.page_id)).resolves.toEqual({
      note_id: shown.page_id, revision_id: "revision_target", relative_path: "Approved.md",
    });
    expect(invoke.mock.calls.map(([operation]) => operation)).toEqual([
      "publication.propose", "publication.show", "publication.approve", "workspace.refresh",
    ]);
  });

  it("cancellation before a decision writes nothing and stale inspection can be fetched again", async () => {
    const shown = { status: "shown", proposal_status: "pending", proposal_id: "proposal_x", page_id: "page_x",
      space_id: "space_x", title: "Title", markdown: "Body", capture_ids: ["capture_x"], evidence: [],
      review_token: "d".repeat(64) };
    const { bridge, invoke } = fake({ "publication.show": [shown, { ...shown, review_token: "e".repeat(64) }] });
    const first = await inspectPublication(bridge, "proposal_x");
    expect(first.review_token).toBe("d".repeat(64));
    const second = await inspectPublication(bridge, "proposal_x");
    expect(second.review_token).toBe("e".repeat(64));
    expect(invoke.mock.calls.every(([operation]) => operation === "publication.show")).toBe(true);
  });

  it("builds an editable inert draft without interpreting hostile text", () => {
    expect(deterministicDraft([capture(0), capture(1)])).toEqual({
      title: "Open Brain publication",
      markdown: "Source 0 <button onclick=evil()>\n\n---\n\nSource 1 <button onclick=evil()>",
    });
  });
});
