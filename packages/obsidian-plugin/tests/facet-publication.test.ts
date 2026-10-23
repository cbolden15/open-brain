import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("obsidian", () => {
  class Element {
    public children: Element[] = [];
    public events: Record<string, () => void> = {};
    public attrs: Record<string, string> = {};
    public text = ""; public value = ""; public disabled = false; public type = ""; public rows = 0;
    public constructor(public tag = "div", options: { text?: string; value?: string } = {}) { this.text = options.text ?? ""; this.value = options.value ?? ""; }
    public createEl(tag: string, options?: { text?: string; value?: string }): Element { const e = new Element(tag, options); this.children.push(e); return e; }
    public createDiv(options?: { text?: string }): Element { return this.createEl("div", options); }
    public createSpan(options?: { text?: string }): Element { return this.createEl("span", options); }
    public addClass(): void {}
    public empty(): void { this.children = []; }
    public setText(value: string): void { this.text = value; }
    public setAttribute(key: string, value: string): void { this.attrs[key] = value; }
    public getAttribute(key: string): string | null { return this.attrs[key] ?? null; }
    public addEventListener(event: string, callback: () => void): void { this.events[event] = callback; }
    public trigger(event: string): void { this.events[event]?.(); }
    public contains(element: unknown): boolean { return element === this || this.children.some((e) => e.contains(element)); }
    public querySelectorAll(): Element[] { return this.children.flatMap((e) => [e, ...e.querySelectorAll()]).filter((e) => e.attrs["aria-label"]); }
    public focus(): void { if (typeof document !== "undefined") (document as unknown as { activeElement: Element }).activeElement = this; }
  }
  class Modal {
    public contentEl = new Element(); public titleEl = new Element();
    public open(): void { (this as unknown as { onOpen(): void }).onOpen(); }
    public close(): void { (this as unknown as { onClose(): void }).onClose(); }
  }
  return { Modal, __Element: Element };
});

import { FacetClient } from "../src/facet-client";
import { FacetPublicationSession, openFacetPublication, type FacetPublicationTarget } from "../src/facet-publication";
import { parseFacetPublicationInspection, type FacetPublicationInspection } from "../src/facet-inspection";
import { FacetWireError } from "../src/facet-wire";

const frozen = JSON.parse(readFileSync(new URL("fixtures/facets/o0-cases.json", import.meta.url), "utf8"));
// Reuse frozen facet DTOs under current runtime admission; keep fixture bytes historical.
frozen.handshake = { ...frozen.handshake, state_schema_version: 22, runtime_session_version: 16 };
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
const id = (prefix: string, n = 1) => `${prefix}_00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const createInspection = (): FacetPublicationInspection => copy(frozen.publication_inspections[0]);
type Element = { children: Element[]; text: string; tag: string; value: string; type: string; disabled: boolean;
  attrs: Record<string, string>; trigger(event: string): void; focus(): void };
const walk = (element: Element): Element[] => [element, ...element.children.flatMap(walk)];

function harness(target?: FacetPublicationTarget) {
  const inspection = createInspection();
  const binding = inspection.facet_selection;
  const query = { query_id: binding.query_id, name: "Synthetic query", selector: copy(binding.selector), archived: false, version: 1, selection_version: 1 };
  const assignment = { subject: { kind: "canonical", id: inspection.page_id }, record_id: inspection.page_id,
    revision_id: id("revision"), version: 1, memberships: [{ value_id: binding.selector.all_value_ids[0], membership_version: 1 }] };
  const capabilitySet = new Set<string>(["space.list", "publication.list", "publication.show", "publication.approve", "publication.reject", "publication.edit_and_approve"]);
  const authority = { hasProposal: false };
  const invoke = vi.fn(async (operation: string, args: Record<string, unknown>): Promise<unknown> => {
    if (operation === "facet.query.get") return { status: "ok", dto_version: 1, item: copy(query) };
    if (operation === "facet.query.attachment.get") return { status: "ok", dto_version: 1, item: { page_id: inspection.page_id, query_id: query.query_id, version: 1 } };
    if (operation === "facet.assignment.get") return { status: "ok", dto_version: 1, item: copy(assignment) };
    if (operation === "facet.history.list") return { status: "ok", dto_version: 1, items: [], next_cursor: null, complete: true };
    if (operation === "publication.list") return { status: "listed", offset: args.offset, next_offset: null, proposals: authority.hasProposal ? [{
      proposal_id: inspection.proposal_id, status: inspection.proposal_status, title: inspection.title, space_id: inspection.space_id,
      operation: inspection.operation, capture_ids: inspection.capture_ids, selected_capture_ids: inspection.selected_capture_ids,
      page_id: inspection.page_id, target_page_id: inspection.target_page_id,
      terminal_decision_id: inspection.proposal_status === "pending" ? null : id("decision"),
    }] : [] };
    if (operation === "space.list") return { status: "listed", spaces: [{ space_id: inspection.space_id, name: "Synthetic destination", slug: "synthetic" }], next_offset: null };
    if (operation === "review.propose_from_facet_query") { authority.hasProposal = true; return { status: "proposed", dto_version: 1, effective_idempotency_key: args.operation_id,
      proposal_id: inspection.proposal_id, page_id: inspection.page_id, space_id: inspection.space_id, target_page_id: inspection.target_page_id,
      operation: inspection.operation, proposal_status: "pending" }; }
    if (operation === "publication.show") return copy(inspection);
    if (operation === "publication.reject") return { status: "rejected", proposal_id: inspection.proposal_id };
    if (operation === "publication.approve" || operation === "publication.edit_and_approve") return {
      status: operation.endsWith("edit_and_approve") ? "edited" : "approved", proposal_id: inspection.proposal_id,
      page_id: inspection.page_id, publication_id: id("publication"),
    };
    throw new Error(`Unexpected ${operation}`);
  });
  const client = new FacetClient({ invoke } as never, frozen.handshake);
  const refreshAndOpen = vi.fn(async (_pageId: string, _cancelled: () => boolean) => {});
  const hooks = { capabilities: capabilitySet, refreshAndOpen };
  const chosen = target ?? { kind: "create", queryId: query.query_id };
  const changed = vi.fn();
  const session = new FacetPublicationSession(client, chosen, hooks, changed);
  const ownerDraft = () => { session.title = inspection.title; session.markdown = inspection.markdown; session.destinationSpaceId = inspection.space_id; };
  const ready = async () => { await session.load(); ownerDraft(); };
  const review = async () => { await ready(); await session.propose(); expect(session.state).toBe("review"); };
  return { session, client, invoke, inspection, query, assignment, hooks, capabilitySet, refreshAndOpen, changed, ownerDraft, ready, review, authority };
}

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("facet publication interaction state (synthetic bridge, not engine proof)", () => {
  it("requires an explicit new destination and an owner draft before dispatch", async () => {
    const h = harness(); await h.session.load();
    expect(h.session.destinationSpaceId).toBe(""); expect(h.session.markdown).toBe("");
    await h.session.propose();
    expect(h.invoke.mock.calls.some(([op]) => op === "review.propose_from_facet_query")).toBe(false);
    h.ownerDraft(); h.session.destinationSpaceId = id("space", 55); await h.session.propose();
    expect(h.session.state).toBe("editing"); expect(h.session.message).toContain("explicit destination");
  });
  it("uses the query freeze operation and parses the actual inspection without routing or visible-page IDs", async () => {
    const h = harness(); await h.review();
    const calls = h.invoke.mock.calls;
    const proposal = calls.find(([op]) => op === "review.propose_from_facet_query")![1];
    expect(Object.keys(proposal).sort()).toEqual(["dto_version", "operation_id", "query_id", "expected_query_version", "target_page_id", "expected_page_revision_id", "destination_space_id", "title", "markdown"].sort());
    expect(proposal).toMatchObject({ query_id: h.query.query_id, destination_space_id: h.inspection.space_id, target_page_id: null });
    expect(calls.map(([op]) => op)).not.toContain("inbox.route"); expect(calls.map(([op]) => op)).not.toContain("publication.propose");
    expect(h.session.inspection).toEqual(parseFacetPublicationInspection(h.inspection));
  });
  it.each([0, 33])("retains the owner draft after refused %i-source proposal", async () => {
    const h = harness(); await h.ready();
    h.invoke.mockImplementationOnce(async () => { throw new FacetWireError("limit_exceeded"); });
    await h.session.propose();
    expect(h.session.state).toBe("editing"); expect(h.session.markdown).toBe(h.inspection.markdown);
    expect(h.session.message).toContain("1–32"); expect(h.session.inspection).toBeNull();
  });
  it("loads an update's attached query/current revision with page facets separate and destination fixed", async () => {
    const initial = createInspection();
    const h = harness({ kind: "update", pageId: initial.page_id, recordId: initial.page_id, spaceId: initial.space_id });
    h.inspection.operation = "update"; h.inspection.target_page_id = h.inspection.page_id;
    h.inspection.expected_page_sha256 = "a".repeat(64); h.inspection.expected_publication_id = id("publication", 2);
    Object.assign(h.inspection.facet_selection, { operation: "update", attachment_version: 1,
      expected_page_sha256: h.inspection.expected_page_sha256, expected_publication_id: h.inspection.expected_publication_id });
    const inherited = id("capture", 9);
    h.inspection.capture_ids.push(inherited); h.inspection.facet_selection.provenance.push(inherited);
    h.inspection.evidence.push({ capture_id: inherited, excerpt: "Inherited evidence", sha256: "b".repeat(64), projection_applied: false });
    h.inspection.facet_selection.source_states.push({ capture_id: inherited, route_id: null, sha256: "c".repeat(64), space_id: id("space", 2) });
    await h.review();
    const request = h.invoke.mock.calls.find(([op]) => op === "review.propose_from_facet_query")![1];
    expect(request).toMatchObject({ query_id: h.query.query_id, target_page_id: initial.page_id,
      expected_page_revision_id: h.assignment.revision_id, destination_space_id: null });
    expect(h.session.pageAssignments?.memberships).toEqual(h.assignment.memberships);
    expect(h.session.inspection?.selected_capture_ids).toHaveLength(1); expect(h.session.inspection?.capture_ids).toHaveLength(2);
    expect(h.invoke.mock.calls.some(([op]) => op === "space.list")).toBe(false);
  });
  it("refuses an update without an attached query", async () => {
    const i = createInspection(); const h = harness({ kind: "update", pageId: i.page_id, recordId: i.page_id, spaceId: i.space_id });
    h.invoke.mockImplementationOnce(async () => ({ status: "ok", dto_version: 1, item: null }));
    await h.session.load(); expect(h.session.state).toBe("recovery_required");
    expect(h.invoke.mock.calls.some(([op]) => op === "review.propose_from_facet_query")).toBe(false);
  });
  it("disables unavailable facet publication without disturbing legacy client methods", async () => {
    const h = harness(); h.capabilitySet.delete("publication.show"); await h.session.load();
    expect(h.session.state).toBe("unavailable"); expect(h.invoke).not.toHaveBeenCalled();
  });
  it("retains a proposal's exact operation ID after timeout and suppresses duplicate submits", async () => {
    const h = harness(); await h.ready();
    const normal = h.invoke.getMockImplementation()!;
    let failed = false;
    h.invoke.mockImplementation(async (op, args) => { if (op === "review.propose_from_facet_query" && !failed) { failed = true; throw new FacetWireError("timeout"); } return normal(op, args); });
    await h.session.propose(); expect(h.session.state).toBe("proposal_uncertain");
    const retained = h.session.proposalMutation!.request;
    await Promise.all([h.session.retryProposal(), h.session.retryProposal()]);
    const requests = h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query").map(([, args]) => args);
    expect(requests).toHaveLength(2); expect(requests[0]).toEqual(requests[1]); expect(h.session.proposalMutation!.request).toBe(retained);
    expect(h.session.state).toBe("review");
  });
  it("retrying failed inspection only reads the committed proposal and never permits a decision first", async () => {
    const h = harness(); await h.ready(); const normal = h.invoke.getMockImplementation()!;
    let bad = true;
    h.invoke.mockImplementation(async (op, args) => op === "publication.show" && bad ? { ...h.inspection, facet_selection: {} } : normal(op, args));
    await h.session.propose(); expect(h.session.state).toBe("inspection_error");
    await h.session.decide("approve"); expect(h.invoke.mock.calls.some(([op]) => op === "publication.approve")).toBe(false);
    bad = false; await h.session.retryInspection(); expect(h.session.state).toBe("review");
    expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
  });
  it("rejects a valid-shaped inspection from a different frozen query", async () => {
    const h = harness(); await h.ready(); h.inspection.facet_selection.query_id = id("facetquery", 77);
    await h.session.propose(); expect(h.session.state).toBe("inspection_error"); expect(h.session.inspection).toBeNull();
  });
  it("keeps the decision token/key/edited draft exact after child loss", async () => {
    const h = harness(); await h.review(); h.session.markdown = "Owner edited draft";
    const normal = h.invoke.getMockImplementation()!; let failed = false;
    h.invoke.mockImplementation(async (op, args) => { if (op === "publication.edit_and_approve" && !failed) { failed = true; throw new FacetWireError("bridge_closed"); } return normal(op, args); });
    await h.session.decide("edit_and_approve"); expect(h.session.state).toBe("decision_uncertain");
    const retained = h.session.decisionRequest;
    await Promise.all([h.session.retryDecision(), h.session.retryDecision()]);
    const requests = h.invoke.mock.calls.filter(([op]) => op === "publication.edit_and_approve").map(([, args]) => args);
    expect(requests).toHaveLength(2); expect(requests[0]).toEqual(requests[1]); expect(requests[0]).toMatchObject({ review_token: h.inspection.review_token, markdown: "Owner edited draft" });
    expect(h.session.decisionRequest).toBe(retained); expect(h.refreshAndOpen).toHaveBeenCalledOnce();
  });
  it("does not silently approve unsubmitted draft edits", async () => {
    const h = harness(); await h.review(); h.session.markdown = "changed"; await h.session.decide("approve");
    expect(h.session.state).toBe("review"); expect(h.session.message).toContain("Edit and approve");
    expect(h.invoke.mock.calls.some(([op]) => op === "publication.approve")).toBe(false);
  });
  it.each(["refresh", "open"])("recovers committed approval after %s failure with refresh/open only", async () => {
    const h = harness(); await h.review(); h.refreshAndOpen.mockRejectedValueOnce(new Error("Synthetic display failure"));
    await h.session.decide("approve"); expect(h.session.state).toBe("refresh_required"); expect(h.session.publication?.page_id).toBe(h.inspection.page_id);
    await h.session.retryRefresh(); expect(h.session.state).toBe("published");
    expect(h.refreshAndOpen).toHaveBeenCalledTimes(2);
    expect(h.invoke.mock.calls.filter(([op]) => op === "publication.approve")).toHaveLength(1);
    expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
  });
  it("rejection never starts refresh/open or records a published page", async () => {
    const h = harness(); await h.review(); await h.session.decide("reject");
    expect(h.session.state).toBe("rejected"); expect(h.session.publication).toBeNull(); expect(h.refreshAndOpen).not.toHaveBeenCalled();
  });
  it("stale evidence keeps the draft and needs an explicit fresh proposal using current query version", async () => {
    const h = harness(); await h.review();
    h.invoke.mockImplementationOnce(async () => { throw new FacetWireError("review_conflict"); });
    await h.session.decide("approve"); expect(h.session.state).toBe("stale"); expect(h.session.markdown).toBe(h.inspection.markdown);
    await h.session.retryInspection(); expect(h.session.state).toBe("stale");
    h.query.version = 2; h.inspection.facet_selection.query_version = 2;
    await h.session.propose(true); expect(h.session.state).toBe("review");
    const requests = h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query").map(([, args]) => args);
    expect(requests[1]!.expected_query_version).toBe(2); expect(requests[0]!.operation_id).not.toBe(requests[1]!.operation_id);
  });
  it("an unknown decision error is uncertain and cannot generate a replacement proposal", async () => {
    const h = harness(); await h.review(); h.invoke.mockImplementationOnce(async () => { throw new Error("Synthetic parse failure"); });
    await h.session.decide("approve"); expect(h.session.state).toBe("decision_uncertain");
    await h.session.propose(true); expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
  });
  it("disposing a held proposal response prevents inspection or refresh continuations", async () => {
    const h = harness(); await h.ready(); const normal = h.invoke.getMockImplementation()!;
    let release!: () => void;
    h.invoke.mockImplementationOnce(async (op, args) => { await new Promise<void>((resolve) => { release = resolve; }); return normal(op, args); });
    const pending = h.session.propose(); h.session.dispose(); const changed = h.changed.mock.calls.length; release(); await pending;
    expect(h.session.state).toBe("disposed"); expect(h.changed).toHaveBeenCalledTimes(changed);
    expect(h.invoke.mock.calls.some(([op]) => op === "publication.show")).toBe(false); expect(h.refreshAndOpen).not.toHaveBeenCalled();
  });
  it("supplies a disposal guard to the refresh/open callback", async () => {
    const h = harness(); await h.review();
    let guard!: () => boolean; let release!: () => void;
    h.refreshAndOpen.mockImplementation(async (_pageId, cancelled) => { guard = cancelled; await new Promise<void>((resolve) => { release = resolve; }); });
    const pending = h.session.decide("approve"); await vi.waitFor(() => expect(h.refreshAndOpen).toHaveBeenCalledOnce());
    expect(guard()).toBe(false); h.session.dispose(); expect(guard()).toBe(true); release(); await pending; expect(h.session.state).toBe("disposed");
  });
});

describe("discarded publication recovery using the actual FacetClient and synthetic authority", () => {
  const reopen = (h: ReturnType<typeof harness>) => new FacetPublicationSession(h.client, { kind: "create", queryId: h.query.query_id }, h.hooks);
  async function loseDecision(h: ReturnType<typeof harness>) {
    await h.review(); h.invoke.mockImplementationOnce(async () => { throw new FacetWireError("timeout"); });
    await h.session.decide("approve"); expect(h.session.state).toBe("decision_uncertain"); h.session.dispose();
  }
  async function loseProposal(h: ReturnType<typeof harness>) {
    await h.ready(); h.invoke.mockImplementationOnce(async () => { throw new FacetWireError("timeout"); });
    h.authority.hasProposal = true;
    await h.session.propose(); expect(h.session.state).toBe("proposal_uncertain"); h.session.dispose();
  }
  it.each(["approved", "edited"] as const)("lost committed %s response reopens into refresh-only recovery without inventing a receipt", async (status) => {
    const h = harness(); await loseDecision(h); h.inspection.proposal_status = status;
    const second = reopen(h); const calls = h.invoke.mock.calls.length; await second.load();
    expect(second.state).toBe("recovery_required"); expect(h.invoke.mock.calls.length).toBe(calls);
    second.title = "Replacement"; second.markdown = "Replacement"; second.destinationSpaceId = h.inspection.space_id;
    await second.propose(); expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
    await second.recover(); expect(second.state).toBe("refresh_required"); expect(second.recoveredPageId).toBe(h.inspection.page_id);
    expect(second.publication).toBeNull(); expect(h.refreshAndOpen).not.toHaveBeenCalled();
    await second.retryRefresh(); expect(second.state).toBe("published"); expect(h.refreshAndOpen).toHaveBeenCalledOnce();
    expect(h.invoke.mock.calls.slice(calls).map(([op]) => op)).toEqual(["publication.show"]);
    await second.propose(true); expect(h.invoke.mock.calls.filter(([op]) => op === "publication.approve")).toHaveLength(1);
    second.dispose(); const third = reopen(h); await third.load(); expect(third.state).toBe("recovery_required");
  });
  it("known pending recovery resumes actual frozen inspection and rejected recovery creates nothing", async () => {
    const h = harness(); await loseDecision(h); const second = reopen(h); await second.load(); await second.recover();
    expect(second.state).toBe("review"); expect(second.inspection?.review_token).toBe(h.inspection.review_token);
    await second.decide("reject"); expect(second.state).toBe("rejected"); expect(h.refreshAndOpen).not.toHaveBeenCalled();
    h.inspection.proposal_status = "rejected"; second.dispose(); const third = reopen(h); await third.load(); await third.recover();
    expect(third.state).toBe("rejected"); expect(third.publication).toBeNull(); expect(h.refreshAndOpen).not.toHaveBeenCalled();
    expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
  });
  it("unknown proposal loss requires listing, parsed inspections and query history, then resumes matching committed proposal", async () => {
    const h = harness(); await loseProposal(h); h.inspection.proposal_status = "approved";
    const second = reopen(h); await second.load(); await second.recover(); expect(second.state).toBe("recovery_review");
    expect(second.recoveryHistoryComplete).toBe(true); expect(second.recoveryListingComplete).toBe(true);
    expect(second.recoveryProposals).toHaveLength(1);
    await second.acknowledgeRecovery(); expect(second.state).toBe("recovery_review");
    second.resumeRecovered(h.inspection.proposal_id); expect(second.state).toBe("refresh_required");
    await second.retryRefresh(); expect(h.refreshAndOpen).toHaveBeenCalledOnce();
    expect(h.invoke.mock.calls.some(([op]) => op === "facet.history.list")).toBe(true);
    expect(h.invoke.mock.calls.some(([op]) => op === "publication.list")).toBe(true);
    expect(h.invoke.mock.calls.filter(([op]) => op === "publication.list").every(([, args]) => args.facet_query_id === h.query.query_id)).toBe(true);
    expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
  });
  it("complete no-match recovery remains blocked until explicit owner acknowledgment", async () => {
    const h = harness(); await loseProposal(h); const normal = h.invoke.getMockImplementation()!;
    h.invoke.mockImplementation(async (op, args) => op === "publication.list" ? { status: "listed", proposals: [], offset: args.offset, next_offset: null } : normal(op, args));
    const second = reopen(h); await second.load(); await second.recover(); expect(second.state).toBe("recovery_review");
    await second.propose(); expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
    await second.acknowledgeRecovery(); expect(second.state).toBe("editing"); expect(second.markdown).toBe(""); expect(second.destinationSpaceId).toBe("");
    second.dispose(); const third = reopen(h); await third.load(); expect(third.state).toBe("editing");
  });
  it("unfinished proposal listing uses explicit next-page recovery and blocks acknowledgment", async () => {
    const h = harness(); await loseProposal(h); const normal = h.invoke.getMockImplementation()!;
    const initialLists = h.invoke.mock.calls.filter(([op]) => op === "publication.list").length;
    h.invoke.mockImplementation(async (op, args) => op === "publication.list" ? { status: "listed", offset: args.offset, next_offset: args.offset === 0 ? 10 : null,
      proposals: args.offset === 0 ? Array.from({ length: 10 }, (_, n) => ({ proposal_id: id("proposal", n + 10), status: "pending", title: "Other page update",
        space_id: h.inspection.space_id, operation: "update", capture_ids: h.inspection.capture_ids, selected_capture_ids: h.inspection.selected_capture_ids,
        page_id: id("page", n + 10), target_page_id: id("page", n + 10), terminal_decision_id: null })) : [] } : normal(op, args));
    const second = reopen(h); await second.load(); await second.recover(); expect(second.recoveryListingComplete).toBe(false);
    await second.acknowledgeRecovery(); expect(second.state).toBe("recovery_review");
    expect(h.invoke.mock.calls.filter(([op]) => op === "publication.list")).toHaveLength(initialLists + 1);
    await second.recover(); expect(second.recoveryListingComplete).toBe(true);
    expect(h.invoke.mock.calls.filter(([op]) => op === "publication.list").slice(initialLists).map(([, args]) => args.offset)).toEqual([0, 10]);
  });
  it.each(["publication.list", "facet.history.list", "publication.show"])("unavailable or failed %s cannot prove recovery or enable replacement", async (failed) => {
    const h = harness(); await loseProposal(h); const normal = h.invoke.getMockImplementation()!;
    h.invoke.mockImplementation(async (op, args) => { if (op === failed) throw new FacetWireError("unsupported_capability"); return normal(op, args); });
    const second = reopen(h); await second.load(); await second.recover(); expect(second.state).toBe("recovery_required");
    await second.acknowledgeRecovery(); await second.propose(true);
    expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
  });
  it("a late pending proposal response updates only the identity marker and never starts closed-view inspection", async () => {
    const h = harness(); await h.ready(); const normal = h.invoke.getMockImplementation()!; let release!: () => void;
    h.invoke.mockImplementationOnce(async (op, args) => { await new Promise<void>((resolve) => { release = resolve; }); return normal(op, args); });
    const pending = h.session.propose(); h.session.dispose();
    const second = reopen(h); await second.load(); await second.recover(); expect(second.state).toBe("recovery_review");
    await second.acknowledgeRecovery(); expect(second.state).toBe("recovery_review");
    const shown = h.invoke.mock.calls.filter(([op]) => op === "publication.show").length;
    release(); await pending; expect(h.session.state).toBe("disposed");
    expect(h.invoke.mock.calls.filter(([op]) => op === "publication.show")).toHaveLength(shown);
    await second.recover(); expect(second.state).toBe("review");
    expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
  });
  it("disposal during authoritative recovery prevents continuation and preserves the blocker", async () => {
    const h = harness(); await loseDecision(h); let release!: (value: unknown) => void;
    h.invoke.mockImplementationOnce(async () => new Promise((resolve) => { release = resolve; }));
    const second = reopen(h); await second.load(); const recovering = second.recover(); second.dispose();
    release(copy(h.inspection)); await recovering; expect(second.state).toBe("disposed");
    const third = reopen(h); await third.load(); expect(third.state).toBe("recovery_required");
    expect(h.refreshAndOpen).not.toHaveBeenCalled();
  });
  it("failed inspection can retry the same bounded recovery page without duplicate-ID poisoning", async () => {
    const h = harness(); await loseProposal(h); const normal = h.invoke.getMockImplementation()!; let fail = true;
    h.invoke.mockImplementation(async (op, args) => { if (op === "publication.show" && fail) { fail = false; throw new FacetWireError("timeout"); } return normal(op, args); });
    const second = reopen(h); await second.load(); await second.recover(); expect(second.state).toBe("recovery_required");
    await second.recover(); expect(second.state).toBe("recovery_review"); expect(second.recoveryProposals).toHaveLength(1);
  });
  it("update recovery is keyed by canonical page and recovers its committed update without reattachment", async () => {
    const initial = createInspection(); const target = { kind: "update", pageId: initial.page_id, recordId: initial.page_id, spaceId: initial.space_id } as const;
    const h = harness(target);
    h.inspection.operation = "update"; h.inspection.target_page_id = h.inspection.page_id;
    h.inspection.expected_page_sha256 = "a".repeat(64); h.inspection.expected_publication_id = id("publication", 2);
    Object.assign(h.inspection.facet_selection, { operation: "update", attachment_version: 1,
      expected_page_sha256: h.inspection.expected_page_sha256, expected_publication_id: h.inspection.expected_publication_id });
    await loseDecision(h); h.inspection.proposal_status = "approved";
    const second = new FacetPublicationSession(h.client, target, h.hooks); const calls = h.invoke.mock.calls.length;
    await second.load(); expect(second.state).toBe("recovery_required"); await second.recover();
    expect(second.state).toBe("refresh_required"); expect(second.recoveredPageId).toBe(target.pageId); expect(second.publication).toBeNull();
    expect(h.invoke.mock.calls.slice(calls).map(([op]) => op)).toEqual(["publication.show"]);
  });
  it("dispose clears drafts, tokens and retained requests while reopening keeps the recovery blocker", async () => {
    const h = harness(); await loseDecision(h);
    expect(h.session.markdown).toBe(""); expect(h.session.title).toBe(""); expect(h.session.inspection).toBeNull();
    expect(h.session.decisionRequest).toBeNull(); expect(h.session.proposalMutation).toBeNull();
    const second = reopen(h); await second.load(); expect(second.state).toBe("recovery_required");
  });
  it("malformed unbound inspection cannot be treated as proof of an unrelated manual proposal", async () => {
    const h = harness(); await loseProposal(h); const normal = h.invoke.getMockImplementation()!;
    h.invoke.mockImplementation(async (op, args) => op === "publication.show" ? { status: "shown", proposal_id: h.inspection.proposal_id } : normal(op, args));
    const second = reopen(h); await second.load(); await second.recover(); expect(second.state).toBe("recovery_required");
    await second.acknowledgeRecovery(); expect(second.state).toBe("recovery_required");
  });
  it("a previously opened editor also blocks replacement when another same-target session discards uncertainty", async () => {
    const h = harness(); const second = reopen(h); await second.load(); expect(second.state).toBe("editing");
    await loseDecision(h); second.title = "Replacement"; second.markdown = "Replacement"; second.destinationSpaceId = h.inspection.space_id;
    await second.propose(); expect(second.state).toBe("recovery_required");
    expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
  });
});

describe("fresh-client publication startup proof", () => {
  function fresh(h: ReturnType<typeof harness>, target?: FacetPublicationTarget) {
    return new FacetPublicationSession(new FacetClient({ invoke: h.invoke } as never, frozen.handshake), target ?? { kind: "create", queryId: h.query.query_id }, h.hooks);
  }
  it("initial empty publication state requires query-filtered listing and history before editing", async () => {
    const h = harness(); await h.session.load(); expect(h.session.state).toBe("editing");
    expect(h.invoke.mock.calls.find(([op]) => op === "publication.list")![1]).toMatchObject({ facet_query_id: h.query.query_id, limit: 10, offset: 0 });
    expect(h.invoke.mock.calls.find(([op]) => op === "facet.history.list")![1]).toMatchObject({ subject: { kind: "query", id: h.query.query_id }, limit: 50 });
    expect(h.invoke.mock.calls.some(([op]) => op === "review.propose_from_facet_query")).toBe(false);
  });
  it("plugin unload after lost committed approval resumes the actual page through fresh-client inspection and refresh only", async () => {
    const h = harness(); await h.review();
    h.invoke.mockImplementationOnce(async () => { h.inspection.proposal_status = "approved"; throw new FacetWireError("timeout"); });
    await h.session.decide("approve"); h.session.dispose(); h.client.dispose();
    const second = fresh(h); await second.load(); expect(second.state).toBe("recovery_review");
    expect(second.recoveryProposals[0]!.proposal_status).toBe("approved");
    await second.propose(); expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
    second.resumeRecovered(h.inspection.proposal_id); expect(second.state).toBe("refresh_required"); expect(second.publication).toBeNull();
    await second.retryRefresh(); expect(h.refreshAndOpen).toHaveBeenCalledOnce();
    expect(h.invoke.mock.calls.filter(([op]) => op === "publication.approve")).toHaveLength(1);
  });
  it("fresh-client lost proposal recovery resumes pending evidence and decides without a replacement freeze", async () => {
    const h = harness(); await h.ready();
    h.invoke.mockImplementationOnce(async () => { h.authority.hasProposal = true; throw new FacetWireError("timeout"); });
    await h.session.propose(); h.session.dispose(); h.client.dispose();
    const second = fresh(h); await second.load(); expect(second.state).toBe("recovery_review");
    await second.acknowledgeRecovery(true); expect(second.state).toBe("recovery_review");
    second.resumeRecovered(h.inspection.proposal_id); expect(second.state).toBe("review");
    await second.decide("approve"); expect(second.state).toBe("published");
    expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
  });
  it("previously approved pages allow another page only through explicit owner intent after complete proof", async () => {
    const h = harness(); h.authority.hasProposal = true; h.inspection.proposal_status = "approved";
    const second = fresh(h); await second.load(); expect(second.state).toBe("recovery_review");
    await second.acknowledgeRecovery(); expect(second.state).toBe("recovery_review");
    await second.acknowledgeRecovery(true); expect(second.state).toBe("editing"); expect(second.markdown).toBe(""); expect(second.destinationSpaceId).toBe("");
    h.inspection.page_id = id("page", 88); h.inspection.proposal_id = id("proposal", 88); h.inspection.proposal_status = "pending";
    h.inspection.facet_selection.page_id = h.inspection.page_id; h.inspection.facet_selection.proposal_id = h.inspection.proposal_id;
    second.title = h.inspection.title; second.markdown = h.inspection.markdown; second.destinationSpaceId = h.inspection.space_id;
    await second.propose(); expect(second.state).toBe("review"); expect(second.inspection?.page_id).toBe(id("page", 88));
    expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
  });
  it("known same-client terminal recovery can review history and deliberately begin another page", async () => {
    const h = harness(); await h.review(); h.invoke.mockImplementationOnce(async () => { throw new FacetWireError("timeout"); });
    await h.session.decide("approve"); h.session.dispose(); h.inspection.proposal_status = "approved";
    const second = new FacetPublicationSession(h.client, { kind: "create", queryId: h.query.query_id }, h.hooks);
    await second.load(); await second.recover(); expect(second.state).toBe("refresh_required");
    await second.reviewPriorPublications(); expect(second.state).toBe("recovery_review");
    expect(second.recoveryHistoryComplete).toBe(true); expect(second.recoveryListingComplete).toBe(true);
    await second.acknowledgeRecovery(true); expect(second.state).toBe("editing");
  });
  it("fresh client stays closed while a disposed client's unknown dispatch is still in flight and rechecks after settlement", async () => {
    const h = harness(); await h.ready(); const normal = h.invoke.getMockImplementation()!; let release!: () => void;
    h.invoke.mockImplementationOnce(async (op, args) => { await new Promise<void>((resolve) => { release = resolve; }); return normal(op, args); });
    const pending = h.session.propose(); h.session.dispose(); h.client.dispose();
    const second = fresh(h); await second.load(); expect(second.state).toBe("recovery_review");
    await second.acknowledgeRecovery(true); expect(second.state).toBe("recovery_review");
    expect(second.recoveryProposals).toHaveLength(0);
    release(); await pending; await second.recover(); expect(second.state).toBe("recovery_review");
    expect(second.recoveryProposals).toHaveLength(1);
    second.resumeRecovered(h.inspection.proposal_id); expect(second.state).toBe("review");
    expect(h.invoke.mock.calls.filter(([op]) => op === "review.propose_from_facet_query")).toHaveLength(1);
  });
  it.each(["publication.list", "facet.history.list"])("startup with failed %s cannot offer a replacement", async (failed) => {
    const h = harness(); const normal = h.invoke.getMockImplementation()!;
    h.invoke.mockImplementation(async (op, args) => { if (op === failed) throw new FacetWireError("unsupported_capability"); return normal(op, args); });
    const second = fresh(h); await second.load(); expect(second.state).toBe("recovery_required");
    await second.propose(); expect(h.invoke.mock.calls.some(([op]) => op === "review.propose_from_facet_query")).toBe(false);
  });
  it("missing listing capability disables startup before any authority reads", async () => {
    const h = harness(); h.capabilitySet.delete("publication.list"); const second = fresh(h); await second.load();
    expect(second.state).toBe("unavailable"); expect(h.invoke).not.toHaveBeenCalled();
  });
  it("query drift between startup proof and fresh input read resets recovery instead of mixing versions", async () => {
    const h = harness(); const normal = h.invoke.getMockImplementation()!; let queryReads = 0;
    h.invoke.mockImplementation(async (op, args) => { if (op === "facet.query.get" && ++queryReads === 2) h.query.version = 2; return normal(op, args); });
    const second = fresh(h); await second.load(); expect(second.state).toBe("recovery_required");
    expect(second.recoveryListingComplete).toBe(false); expect(second.recoveryHistoryComplete).toBe(false);
    await second.propose(); expect(h.invoke.mock.calls.some(([op]) => op === "review.propose_from_facet_query")).toBe(false);
    await second.recover(); expect(second.state).toBe("editing"); expect(second.query?.version).toBe(2);
  });
  it("oversized startup listing refuses proof rather than silently trimming candidates", async () => {
    const h = harness(); const normal = h.invoke.getMockImplementation()!;
    h.invoke.mockImplementation(async (op, args) => op === "publication.list" ? { status: "listed", offset: 0, next_offset: null, proposals: Array(11).fill({}) } : normal(op, args));
    const second = fresh(h); await second.load(); expect(second.state).toBe("recovery_required");
    await second.acknowledgeRecovery(true); expect(second.state).toBe("recovery_required");
  });
  it("fresh update startup rereads attachment/current revision and resumes a prior committed update", async () => {
    const initial = createInspection(); const target = { kind: "update", pageId: initial.page_id, recordId: initial.page_id, spaceId: initial.space_id } as const;
    const h = harness(target); h.authority.hasProposal = true; h.inspection.operation = "update"; h.inspection.target_page_id = h.inspection.page_id;
    h.inspection.proposal_status = "approved"; h.inspection.expected_page_sha256 = "a".repeat(64); h.inspection.expected_publication_id = id("publication", 2);
    Object.assign(h.inspection.facet_selection, { operation: "update", attachment_version: 1,
      expected_page_sha256: h.inspection.expected_page_sha256, expected_publication_id: h.inspection.expected_publication_id });
    const second = fresh(h, target); await second.load(); expect(second.state).toBe("recovery_review");
    expect(second.pageAssignments?.revision_id).toBe(h.assignment.revision_id);
    expect(h.invoke.mock.calls.find(([op]) => op === "publication.list")![1].facet_query_id).toBe(h.query.query_id);
    second.resumeRecovered(h.inspection.proposal_id); expect(second.state).toBe("refresh_required"); expect(second.recoveredPageId).toBe(target.pageId);
  });
  it("a conflict on recovered pending evidence requires new authority inspection before any replacement", async () => {
    const h = harness(); h.authority.hasProposal = true; const second = fresh(h); await second.load();
    second.resumeRecovered(h.inspection.proposal_id);
    h.invoke.mockImplementationOnce(async () => { h.inspection.proposal_status = "approved"; throw new FacetWireError("review_conflict"); });
    await second.decide("approve"); expect(second.state).toBe("recovery_required");
    await second.propose(true); expect(h.invoke.mock.calls.some(([op]) => op === "review.propose_from_facet_query")).toBe(false);
    await second.recover(); expect(second.state).toBe("recovery_review"); expect(second.recoveryProposals[0]!.proposal_status).toBe("approved");
  });
});

describe("actual facet publication modal with synthetic DOM", () => {
  it("renders text safely, labels native keyboard controls, and restores opening focus", async () => {
    const obsidian = await import("obsidian") as unknown as { __Element: new () => Element };
    vi.stubGlobal("HTMLElement", obsidian.__Element);
    const opener = new obsidian.__Element(); vi.stubGlobal("document", { activeElement: opener });
    const h = harness(); h.query.name = "<script>synthetic()</script>";
    const modal = openFacetPublication({} as never, h.client, { kind: "create", queryId: h.query.query_id }, h.hooks);
    await vi.waitFor(() => expect(modal.session.state).toBe("editing"));
    const elements = walk(modal.contentEl as unknown as Element);
    expect(elements.some((el) => el.text.includes("<script>synthetic()</script>"))).toBe(true);
    expect(elements.some((el) => el.tag === "script")).toBe(false);
    expect(elements.find((el) => el.attrs["aria-label"] === "Owner-written Markdown draft")?.tag).toBe("textarea");
    expect(elements.find((el) => el.text === "Create proposal and inspect")?.type).toBe("button");
    expect(elements.find((el) => el.attrs.role === "status")?.attrs["aria-live"]).toBe("polite");
    const draft = elements.find((el) => el.attrs["aria-label"] === "Owner-written Markdown draft")!;
    draft.value = "Typed owner draft"; draft.trigger("input"); expect(modal.session.markdown).toBe("Typed owner draft");
    draft.focus(); modal.close(); expect(document.activeElement).toBe(opener); expect(modal.session.state).toBe("disposed");
  });
  it("warns before close discards an uncertain request and allows explicit discard", async () => {
    const h = harness(); const modal = openFacetPublication({} as never, h.client, { kind: "create", queryId: h.query.query_id }, h.hooks);
    await vi.waitFor(() => expect(modal.session.state).toBe("editing"));
    modal.session.state = "decision_uncertain"; modal.close();
    expect(modal.session.state).toBe("decision_uncertain");
    const discard = walk(modal.contentEl as unknown as Element).find((el) => el.text === "Discard draft and close")!;
    expect(discard).toBeDefined(); discard.trigger("click"); expect(modal.session.state).toBe("disposed");
  });
  it("dispose ignores late initial reads without killing the shared bridge", async () => {
    const h = harness(); let release!: (value: unknown) => void;
    h.invoke.mockImplementationOnce(async () => new Promise((resolve) => { release = resolve; }));
    const modal = openFacetPublication({} as never, h.client, { kind: "create", queryId: h.query.query_id }, h.hooks);
    modal.dispose(); release({ status: "ok", dto_version: 1, item: h.query });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(modal.session.state).toBe("disposed"); expect(walk(modal.contentEl as unknown as Element)).toHaveLength(1);
    expect(h.client.supports("facet.query.get")).toBe(true); expect(h.invoke.mock.calls.some(([op]) => op === "space.list")).toBe(false);
  });
  it("native controls submit the owner draft and show actual frozen evidence before approval", async () => {
    const h = harness(); h.inspection.evidence[0]!.excerpt = "<script>hostile synthetic source</script>";
    const modal = openFacetPublication({} as never, h.client, { kind: "create", queryId: h.query.query_id }, h.hooks);
    await vi.waitFor(() => expect(modal.session.state).toBe("editing"));
    const elements = walk(modal.contentEl as unknown as Element);
    for (const [label, value] of [["Owner-written title", h.inspection.title], ["Owner-written Markdown draft", h.inspection.markdown], ["Destination space", h.inspection.space_id]]) {
      const field = elements.find((e) => e.attrs["aria-label"] === label)!;
      field.value = value!; field.trigger(label === "Destination space" ? "change" : "input");
    }
    elements.find((e) => e.text === "Create proposal and inspect")!.trigger("click");
    await vi.waitFor(() => expect(modal.session.state).toBe("review"));
    const review = walk(modal.contentEl as unknown as Element);
    expect(review.some((e) => e.text === "Frozen proposal Markdown")).toBe(true);
    expect(review.some((e) => e.text === "Newly selected evidence")).toBe(true);
    expect(review.some((e) => e.text === "Inherited provenance")).toBe(true);
    expect(review.some((e) => e.tag === "pre" && e.text === "<script>hostile synthetic source</script>")).toBe(true);
    expect(review.some((e) => e.tag === "script")).toBe(false);
    review.find((e) => e.text === "Approve")!.trigger("click");
    await vi.waitFor(() => expect(modal.session.state).toBe("published"));
    expect(h.refreshAndOpen).toHaveBeenCalledOnce(); modal.dispose();
  });
  it("closing during a paged destination read prevents the next space request", async () => {
    const h = harness(); const normal = h.invoke.getMockImplementation()!;
    let release!: (value: unknown) => void;
    h.invoke.mockImplementation(async (op, args) => op === "space.list" ? new Promise((resolve) => { release = resolve; }) : normal(op, args));
    const modal = openFacetPublication({} as never, h.client, { kind: "create", queryId: h.query.query_id }, h.hooks);
    await vi.waitFor(() => expect(h.invoke.mock.calls.some(([op]) => op === "space.list")).toBe(true));
    modal.dispose(); release({ status: "listed", spaces: [], next_offset: 50 });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(h.invoke.mock.calls.filter(([op]) => op === "space.list")).toHaveLength(1); expect(modal.session.state).toBe("disposed");
  });
  it("reopened modal exposes explicit recovery and refresh-only native controls for a lost committed decision", async () => {
    const h = harness(); await h.review(); h.invoke.mockImplementationOnce(async () => { throw new FacetWireError("timeout"); });
    await h.session.decide("approve"); h.session.dispose(); h.inspection.proposal_status = "approved";
    const modal = openFacetPublication({} as never, h.client, { kind: "create", queryId: h.query.query_id }, h.hooks);
    expect(modal.session.state).toBe("recovery_required");
    const controls = walk(modal.contentEl as unknown as Element);
    expect(controls.some((e) => e.text === "Create proposal and inspect")).toBe(false);
    controls.find((e) => e.text === "Recover authoritative publication state")!.trigger("click");
    await vi.waitFor(() => expect(modal.session.state).toBe("refresh_required"));
    const recovered = walk(modal.contentEl as unknown as Element);
    expect(recovered.some((e) => e.text === "Approve")).toBe(false);
    recovered.find((e) => e.text === "Retry refresh and open")!.trigger("click");
    await vi.waitFor(() => expect(modal.session.state).toBe("published"));
    expect(h.invoke.mock.calls.filter(([op]) => op === "publication.approve")).toHaveLength(1); modal.dispose();
  });
  it("native startup controls require deliberate new-page intent after reviewing a prior committed page", async () => {
    const h = harness(); h.authority.hasProposal = true; h.inspection.proposal_status = "approved";
    const modal = openFacetPublication({} as never, new FacetClient({ invoke: h.invoke } as never, frozen.handshake), { kind: "create", queryId: h.query.query_id }, h.hooks);
    await vi.waitFor(() => expect(modal.session.state).toBe("recovery_review"));
    const controls = walk(modal.contentEl as unknown as Element);
    expect(controls.some((e) => e.text === "Create proposal and inspect")).toBe(false);
    controls.find((e) => e.text === "Start another page from this query")!.trigger("click");
    await vi.waitFor(() => expect(modal.session.state).toBe("editing"));
    expect(modal.session.markdown).toBe(""); expect(modal.session.destinationSpaceId).toBe("");
    expect(h.invoke.mock.calls.some(([op]) => op === "review.propose_from_facet_query")).toBe(false); modal.dispose();
  });
});
