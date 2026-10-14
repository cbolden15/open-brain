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
  const capabilitySet = new Set<string>(["space.list", "publication.show", "publication.approve", "publication.reject", "publication.edit_and_approve"]);
  const invoke = vi.fn(async (operation: string, args: Record<string, unknown>): Promise<unknown> => {
    if (operation === "facet.query.get") return { status: "ok", dto_version: 1, item: copy(query) };
    if (operation === "facet.query.attachment.get") return { status: "ok", dto_version: 1, item: { page_id: inspection.page_id, query_id: query.query_id, version: 1 } };
    if (operation === "facet.assignment.get") return { status: "ok", dto_version: 1, item: copy(assignment) };
    if (operation === "space.list") return { status: "listed", spaces: [{ space_id: inspection.space_id, name: "Synthetic destination", slug: "synthetic" }], next_offset: null };
    if (operation === "review.propose_from_facet_query") return { status: "proposed", dto_version: 1, effective_idempotency_key: args.operation_id,
      proposal_id: inspection.proposal_id, page_id: inspection.page_id, space_id: inspection.space_id, target_page_id: inspection.target_page_id,
      operation: inspection.operation, proposal_status: "pending" };
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
  return { session, client, invoke, inspection, query, assignment, hooks, capabilitySet, refreshAndOpen, changed, ownerDraft, ready, review };
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
    await h.session.load(); expect(h.session.state).toBe("error");
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
});
