import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("obsidian", () => {
  const opened: unknown[] = [];
  class Element {
    public children: Element[] = []; public parent: Element | null = null;
    public tag: string; public text: string; public value = ""; public disabled = false; public checked = false;
    public type = ""; public attributes: Record<string, string> = {}; public events: Record<string, () => void> = {};
    public focused = false;
    public constructor(tag = "div", options: { text?: string; attr?: Record<string, string> } = {}) { this.tag = tag; this.text = options.text ?? ""; this.attributes = options.attr ?? {}; }
    public createEl(tag: string, options?: { text?: string; attr?: Record<string, string> }): Element { const child = new Element(tag, options); child.parent = this; this.children.push(child); return child; }
    public createDiv(options?: { text?: string }): Element { return this.createEl("div", options); }
    public createSpan(options?: { text?: string }): Element { return this.createEl("span", options); }
    public addEventListener(name: string, callback: () => void): void { this.events[name] = callback; }
    public addClass(): void {} public setAttribute(key: string, value: string): void { this.attributes[key] = value; }
    public empty(): void { this.children = []; } public setText(value: string): void { this.text = value; }
    public focus(): void { this.focused = true; } public remove(): void { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); }
    public trigger(name: string): void { if (!this.disabled) this.events[name]?.(); }
  }
  class Modal {
    public contentEl = new Element(); public titleEl = new Element(); public app: unknown;
    public constructor(app: unknown) { this.app = app; }
    public open(): void { opened.push(this); (this as { onOpen?: () => void }).onOpen?.(); }
    public close(): void { (this as { onClose?: () => void }).onClose?.(); }
  }
  class ItemView { public contentEl = new Element(); public app = {}; public constructor(public leaf: unknown) {} }
  return { Modal, ItemView, __opened: opened };
});

import { FacetClient } from "../src/facet-client";
import { FACET_OPERATIONS, facetLimits } from "../src/facet-wire";
import type { Wire_facet_result, Wire_assignment_state, Wire_saved_query, Wire_selector } from "../src/facet-types";
type Element = { tag: string; text: string; value: string; checked: boolean; disabled: boolean; children: Element[]; attributes: Record<string, string>; trigger(name: string): void };
const walk = (element: Element): Element[] => [element, ...element.children.flatMap(walk)];
const rootOf = (modal: { contentEl: HTMLElement }): Element => modal.contentEl as unknown as Element;
const button = (modal: { contentEl: HTMLElement }, label: string): Element => {
  const element = walk(rootOf(modal)).find((el) => el.tag === "button" && el.text === label); expect(element).toBeDefined(); return element!;
};
const checkbox = (modal: { contentEl: HTMLElement }, label: string): Element => {
  const element = walk(rootOf(modal)).find((el) => el.tag === "label" && el.children.some((child) => child.text === label)); expect(element).toBeDefined(); return element!.children.find((child) => child.tag === "input")!;
};
const click = (modal: { contentEl: HTMLElement }, label: string): void => button(modal, label).trigger("click");
const toggle = (modal: { contentEl: HTMLElement }, label: string, checked = true): void => { const input = checkbox(modal, label); input.checked = checked; input.trigger("change"); };
const texts = (modal: { contentEl: HTMLElement }): string[] => walk(rootOf(modal)).map((el) => el.text);
const id = (prefix: string, n: number): string => `${prefix}_123e4567-e89b-42d3-a456-${n.toString(16).padStart(12, "0")}`;
const dimension = { dimension_id: id("facetdim", 1), name: "Topic <script>", archived: false, version: 2 };
const value = { value_id: id("facetval", 2), dimension_id: dimension.dimension_id, name: "One", archived: false, effective_archived: false, version: 1 };
const value2 = { ...value, value_id: id("facetval", 3), name: "Two" };
const archived = { ...value, value_id: id("facetval", 4), name: "Archived", archived: true, effective_archived: true };
const label = (v: typeof value): string => `${dimension.name}: ${v.name}${v.effective_archived ? " (archived)" : ""}`;
const selector: Wire_selector = { all_value_ids: [value.value_id], record_types: [], payload_families: [], space_ids: [] };
const query: Wire_saved_query = { query_id: id("facetquery", 5), name: "Saved", version: 2, selection_version: 2, selector, archived: false };
const state: Wire_assignment_state = { subject: { kind: "source", id: id("source", 6) }, record_id: id("capture", 7), revision_id: id("capture", 7), version: 2, memberships: [{ value_id: archived.value_id, membership_version: 1 }] };
const result = (n: number): Wire_facet_result => ({ subject: { kind: "source", id: id("source", 10000 + n) }, classification_version: 1,
  record: { record_id: id("capture", 1000 + n), revision_id: id("capture", 1000 + n), record_type: "source", source_id: id("source", 10000 + n),
    title: `Title ${n} <script>`, excerpt: `Excerpt ${n}`, payload_family: "text", space_id: null, trust: "third_party", source_update_available: false,
    provenance: { representative_capture_id: id("capture", 1000 + n), capture_ids: [id("capture", 1000 + n)], source_origin: "third_party" } } });
const page = (items: unknown[], next_cursor: string | null = null) => ({ dto_version: 1, status: "ok", items, next_cursor, complete: next_cursor === null });
function harness(overrides: (operation: string, args: Record<string, unknown>) => unknown = () => undefined, operations = [...FACET_OPERATIONS]) {
  const invoke = vi.fn(async (operation: string, args: Record<string, unknown>) => {
    const custom = overrides(operation, args); if (custom !== undefined) return await custom;
    if (operation === "facet.dimension.list") return page([dimension]);
    if (operation === "facet.value.list") return page([value, value2, archived]);
    if (operation === "facet.assignment.get") return { dto_version: 1, status: "ok", item: state };
    if (operation === "facet.query.get") return { dto_version: 1, status: "ok", item: query };
    if (operation === "facet.query.list") return page([query]);
    if (operation === "facet.query.page") return page([result(1)]);
    if (operation === "facet.query.attachment.get") return { dto_version: 1, status: "ok", item: null };
    if (operation === "facet.history.list") return page([]);
    if (/\.(create|rename|archive|update|patch|attach)$/.test(operation)) return { dto_version: 1, status: "applied", operation_id: args.operation_id,
      subject_id: operation.startsWith("facet.query") ? query.query_id : operation.startsWith("facet.dimension") ? dimension.dimension_id : value.value_id,
      version: 3, event_id: id("facetevent", 20) };
    throw new Error(`Unexpected ${operation}`);
  });
  const client = new FacetClient({ invoke } as never, { status: "ok", protocol: "open-brain-client", protocol_version: 1,
    product_version: "0.1.0", desktop_only: true, state_schema_version: 21, runtime_session_version: 16, operations, facet_capabilities: { version: 1, operations: operations.map((operation) => ({ operation, dto_versions: [1], ...facetLimits(operation) })) } });
  return { client, invoke, writes: () => invoke.mock.calls.filter(([op]) => /\.(create|rename|archive|update|patch|attach)$/.test(op)) };
}
const tick = async (): Promise<void> => { await new Promise((resolve) => setTimeout(resolve, 0)); };

import { FacetAssignmentModal, FacetVocabularyModal, FacetVocabularyEditModal, FacetQueryEditorModal, ClassificationHistoryModal, disposeFacetDialogs } from "../src/facet-editor";
afterEach(() => vi.unstubAllGlobals());
beforeEach(async () => { const obsidian = await import("obsidian") as unknown as { __opened: unknown[] }; obsidian.__opened.length = 0; });
const waitAssignment = async (modal: FacetAssignmentModal): Promise<void> => { await vi.waitFor(() => expect(texts(modal)).toContain(label(value))); };
const waitQuery = async (modal: FacetQueryEditorModal): Promise<void> => { await vi.waitFor(() => expect(texts(modal)).toContain("Match all selected values")); };

describe("facet editor interactions (UI proof only)", () => {
  it("Cancel writes nothing and existing archived memberships can only be kept or removed", async () => {
    const h = harness(); const modal = new FacetAssignmentModal({} as never, h.client, state.record_id); modal.open(); await waitAssignment(modal);
    expect(checkbox(modal, label(archived)).disabled).toBe(false);
    toggle(modal, label(value)); click(modal, "Cancel"); expect(h.writes()).toHaveLength(0); expect(rootOf(modal).children).toEqual([]);
    const empty = harness((op) => op === "facet.assignment.get" ? { dto_version: 1, status: "ok", item: { ...state, memberships: [] } } : undefined);
    const second = new FacetAssignmentModal({} as never, empty.client, state.record_id); second.open(); await waitAssignment(second);
    expect(checkbox(second, label(archived)).disabled).toBe(true);
  });
  it("sends one explicit patch using engine identity, classification version and revision", async () => {
    const h = harness(); const modal = new FacetAssignmentModal({} as never, h.client, state.record_id); modal.open(); await waitAssignment(modal);
    toggle(modal, label(value)); toggle(modal, label(archived), false); click(modal, "Save"); click(modal, "Save");
    await vi.waitFor(() => expect(h.writes()).toHaveLength(1));
    expect(h.writes()[0]![1]).toMatchObject({ record_id: state.record_id, expected_revision_id: state.revision_id, expected_version: 2,
      add_value_ids: [value.value_id], remove_value_ids: [archived.value_id] });
  });
  it("preserves stale drafts alongside current state and requires fresh explicit Save", async () => {
    let read = 0, write = 0;
    const fresh = { ...state, version: 7, revision_id: id("capture", 8), record_id: id("capture", 8), memberships: [{ value_id: value2.value_id, membership_version: 2 }] };
    const h = harness((op) => {
      if (op === "facet.assignment.get") return { dto_version: 1, status: "ok", item: ++read === 1 ? state : fresh };
      if (op === "facet.assignment.patch" && ++write === 1) return Promise.reject({ code: "version_conflict" });
      return undefined;
    });
    const modal = new FacetAssignmentModal({} as never, h.client, state.record_id); modal.open(); await waitAssignment(modal);
    toggle(modal, label(value)); toggle(modal, label(archived), false); click(modal, "Save");
    await vi.waitFor(() => expect(texts(modal).some((text) => text.startsWith("Current classification version 7"))).toBe(true));
    expect(checkbox(modal, label(value)).checked).toBe(true); expect(button(modal, "Save").disabled).toBe(true);
    click(modal, "Reload and reapply draft"); await vi.waitFor(() => expect(button(modal, "Save").disabled).toBe(false));
    expect(h.writes()).toHaveLength(1); expect(checkbox(modal, label(value2)).checked).toBe(true);
    click(modal, "Save"); await vi.waitFor(() => expect(h.writes()).toHaveLength(2));
    expect(h.writes()[1]![1]).toMatchObject({ expected_version: 7, expected_revision_id: fresh.revision_id, record_id: fresh.record_id, add_value_ids: [value.value_id], remove_value_ids: [] });
  });
  it("uncertain writes retain the exact request, prevent replacement and warn before discarding", async () => {
    let attempts = 0;
    const h = harness((op) => op === "facet.assignment.patch" && ++attempts === 1 ? Promise.reject({ code: "timeout" }) : undefined);
    const modal = new FacetAssignmentModal({} as never, h.client, state.record_id); modal.open(); await waitAssignment(modal);
    toggle(modal, label(value)); click(modal, "Save");
    await vi.waitFor(() => expect(texts(modal)).toContain("Retry same operation"));
    expect(button(modal, "Save").disabled).toBe(true);
    click(modal, "Cancel"); expect(texts(modal)).toContain("Discard retry state and close");
    click(modal, "Keep dialog open"); click(modal, "Retry same operation");
    await vi.waitFor(() => expect(h.writes()).toHaveLength(2)); expect(h.writes()[1]![1]).toEqual(h.writes()[0]![1]);
  });
  it("closing pending reads prevents late rendering and follow-up vocabulary reads", async () => {
    let release!: (value: unknown) => void;
    const h = harness((op) => op === "facet.assignment.get" ? new Promise((resolve) => { release = resolve; }) : undefined);
    const modal = new FacetAssignmentModal({} as never, h.client, state.record_id); modal.open(); click(modal, "Reload"); modal.close();
    release({ dto_version: 1, status: "ok", item: state }); await tick();
    expect(rootOf(modal).children).toEqual([]); expect(h.invoke.mock.calls.some(([op]) => op === "facet.dimension.list")).toBe(false);
  });
  it("archive asks for explicit confirmation, preserves expected version, and Cancel has no effect", async () => {
    const h = harness(); const cancelled = new FacetVocabularyEditModal({} as never, h.client, { kind: "value", action: "archive", value }, () => {}); cancelled.open();
    expect(texts(cancelled).some((text) => text.includes("Stage 1 has no unarchive"))).toBe(true);
    click(cancelled, "Cancel"); expect(h.writes()).toHaveLength(0);
    const modal = new FacetVocabularyEditModal({} as never, h.client, { kind: "value", action: "archive", value }, () => {}); modal.open(); click(modal, "Confirm archive");
    await vi.waitFor(() => expect(h.writes()).toHaveLength(1)); expect(h.writes()[0]![1]).toMatchObject({ expected_version: value.version, value_id: value.value_id });
    await tick(); expect(button(modal, "Confirm archive").disabled).toBe(true);
  });
  it.each(["dimension", "value"] as const)("create and rename %s carry names and stable identity", async (kind) => {
    const h = harness();
    const create = new FacetVocabularyEditModal({} as never, h.client, { kind, action: "create", dimension }, () => {}); create.open();
    const input = walk(rootOf(create)).find((e) => e.tag === "input")!; input.value = "Owner name"; input.trigger("input"); click(create, "Save");
    await vi.waitFor(() => expect(h.writes()).toHaveLength(1)); expect(h.writes()[0]![1]).toMatchObject({ expected_version: 0, name: "Owner name" });
    const rename = new FacetVocabularyEditModal({} as never, h.client, { kind, action: "rename", dimension, value }, () => {}); rename.open();
    const name = walk(rootOf(rename)).find((e) => e.tag === "input")!; name.value = "Renamed"; name.trigger("input"); click(rename, "Save");
    await vi.waitFor(() => expect(h.writes()).toHaveLength(2));
    expect(h.writes()[1]![1]).toMatchObject(kind === "dimension" ? { dimension_id: dimension.dimension_id, expected_version: dimension.version } : { value_id: value.value_id, expected_version: value.version });
  });
  it("vocabulary manager escapes labels as text and shows item history actions", async () => {
    const h = harness(); const modal = new FacetVocabularyModal({} as never, h.client); modal.open();
    await vi.waitFor(() => expect(texts(modal)).toContain(dimension.name));
    expect(texts(modal)).toContain("Classification history"); expect(walk(rootOf(modal)).some((el) => el.tag === "script")).toBe(false);
    expect(walk(rootOf(modal)).some((el) => el.attributes.role === "status")).toBe(true);
  });
  it("query creation retains all-values semantics and record, payload and space filters", async () => {
    const h = harness(); const space = { space_id: id("space", 30), name: "Selected space" };
    const modal = new FacetQueryEditorModal({} as never, h.client, null, { ...selector, all_value_ids: [] }, { spaces: [space] }); modal.open(); await waitQuery(modal);
    const name = walk(rootOf(modal)).find((el) => el.tag === "input")!;
    name.value = "All of these"; name.trigger("input"); toggle(modal, label(value)); toggle(modal, label(value2)); toggle(modal, "source"); toggle(modal, "text"); toggle(modal, space.name);
    click(modal, "Save"); await vi.waitFor(() => expect(h.writes()).toHaveLength(1));
    expect(h.writes()[0]![1]).toMatchObject({ name: "All of these", selector: { all_value_ids: [value.value_id, value2.value_id], record_types: ["source"], payload_families: ["text"], space_ids: [space.space_id] } });
  });
  it("query stale save preserves edited name and selection until explicit version reload", async () => {
    const h = harness((op) => op === "facet.query.update" ? Promise.reject({ code: "version_conflict" }) : op === "facet.query.get" ? { dto_version: 1, status: "ok", item: { ...query, version: 8 } } : undefined);
    const modal = new FacetQueryEditorModal({} as never, h.client, query); modal.open(); await waitQuery(modal);
    const name = walk(rootOf(modal)).find((el) => el.tag === "input")!; name.value = "My draft"; name.trigger("input"); toggle(modal, label(value2)); click(modal, "Save");
    await vi.waitFor(() => expect(texts(modal)).toContain("Reload versions and keep draft"));
    expect(walk(rootOf(modal)).find((el) => el.tag === "input")!.value).toBe("My draft"); expect(checkbox(modal, label(value2)).checked).toBe(true);
    click(modal, "Reload versions and keep draft"); expect(h.writes()).toHaveLength(1); click(modal, "Save");
    await vi.waitFor(() => expect(h.writes()).toHaveLength(2)); expect(h.writes()[1]![1]).toMatchObject({ expected_version: 8, name: "My draft" });
  });
  it("query attachment is separate from assignment and requires explicit confirmation", async () => {
    const h = harness(); const pageId = id("page", 40), revisionId = id("revision", 41);
    const modal = new FacetQueryEditorModal({} as never, h.client, query, selector, { getAttachmentTarget: async () => ({ pageId, revisionId }) }); modal.open(); await waitQuery(modal);
    click(modal, "Attach to current managed page"); await vi.waitFor(() => expect(texts(modal)).toContain("Confirm attachment")); expect(h.writes()).toHaveLength(0);
    click(modal, "Cancel attachment"); expect(h.writes()).toHaveLength(0); click(modal, "Attach to current managed page"); await tick(); click(modal, "Confirm attachment");
    await vi.waitFor(() => expect(h.writes()).toHaveLength(1)); expect(h.writes()[0]![0]).toBe("facet.query.attach");
    expect(h.writes()[0]![1]).toMatchObject({ page_id: pageId, expected_revision_id: revisionId, expected_version: 0, expected_query_version: query.version });
  });
  it("classification history is read only, paged by explicit action, and labelled separately", async () => {
    const h = harness((op) => op === "facet.history.list" ? page([{ event_id: id("facetevent", 45), sequence: 12, recorded_at: "2026-10-13T12:00:00.000000Z", actor_id: id("actor", 46), kind: "assignment.patch", changes: [{ entity: { kind: "subject", id: state.subject }, before: null, after: { subject_kind: "source", subject_id: state.subject.id, version: 1 } }] }], "history-next") : undefined);
    const modal = new ClassificationHistoryModal({} as never, h.client, state.subject); modal.open();
    await vi.waitFor(() => expect(texts(modal).some((text) => text.includes("Event 12"))).toBe(true));
    expect(h.invoke.mock.calls.filter(([op]) => op === "facet.history.list")).toHaveLength(1); click(modal, "Load more history");
    await vi.waitFor(() => expect(h.invoke.mock.calls.filter(([op]) => op === "facet.history.list")).toHaveLength(2)); expect(h.writes()).toHaveLength(0);
  });
});

describe("facet dialog boundary controls", () => {
  it("returns focus after close and uses labelled native inputs and buttons for keyboard access", async () => {
    const focus = vi.fn(); vi.stubGlobal("document", { activeElement: { focus } });
    const h = harness(); const modal = new FacetQueryEditorModal({} as never, h.client, query); modal.open(); await waitQuery(modal);
    expect(walk(rootOf(modal)).filter((el) => el.tag === "label").every((el) => el.children.some((child) => child.tag === "input"))).toBe(true);
    expect(walk(rootOf(modal)).filter((el) => el.tag === "button").every((el) => !!el.text)).toBe(true);
    modal.close(); expect(focus).toHaveBeenCalledOnce();
  });
  it("unload disposes a pending mutation without late callbacks or UI updates", async () => {
    let release!: (value: unknown) => void; const onChanged = vi.fn();
    const h = harness((op) => op === "facet.assignment.patch" ? new Promise((resolve) => { release = resolve; }) : undefined);
    const modal = new FacetAssignmentModal({} as never, h.client, state.record_id, onChanged); modal.open(); await waitAssignment(modal); toggle(modal, label(value)); click(modal, "Save");
    expect(texts(modal).some((text) => text.includes("unloading discards"))).toBe(true); disposeFacetDialogs(h.client);
    const args = h.writes()[0]![1]; release({ dto_version: 1, status: "applied", operation_id: args.operation_id, subject_id: state.subject.id, version: 3, event_id: id("facetevent", 21) }); await tick();
    expect(rootOf(modal).children).toEqual([]); expect(onChanged).not.toHaveBeenCalled(); expect(h.writes()).toHaveLength(1);
  });
  it("closing attachment preparation performs no write or subsequent reads", async () => {
    let release!: (value: { pageId: string; revisionId: string }) => void;
    const h = harness(); const modal = new FacetQueryEditorModal({} as never, h.client, query, selector, { getAttachmentTarget: () => new Promise((resolve) => { release = resolve; }) }); modal.open(); await waitQuery(modal);
    click(modal, "Attach to current managed page"); click(modal, "Cancel"); release({ pageId: id("page", 40), revisionId: id("revision", 41) }); await tick();
    expect(rootOf(modal).children).toEqual([]); expect(h.writes()).toHaveLength(0); expect(h.invoke.mock.calls.some(([op]) => op === "facet.query.attachment.get")).toBe(false);
  });
  it("query archive and rename preserve the selector; cancel archive has no effect", async () => {
    const h = harness(); const modal = new FacetQueryEditorModal({} as never, h.client, query); modal.open(); await waitQuery(modal);
    click(modal, "Archive query"); expect(texts(modal)).toContain("Confirm archive"); click(modal, "Cancel archive"); expect(h.writes()).toHaveLength(0);
    const name = walk(rootOf(modal)).find((el) => el.tag === "input")!; name.value = "Renamed only"; name.trigger("input"); click(modal, "Save");
    await vi.waitFor(() => expect(h.writes()).toHaveLength(1)); expect(h.writes()[0]![1]).toMatchObject({ name: "Renamed only", query_id: query.query_id, expected_version: query.version, selector });
  });
  it("missing mutation capability visibly disables the action", async () => {
    const operations = FACET_OPERATIONS.filter((op) => op !== "facet.assignment.patch"); const h = harness(() => undefined, operations);
    const modal = new FacetAssignmentModal({} as never, h.client, state.record_id); modal.open(); await waitAssignment(modal); expect(button(modal, "Save").disabled).toBe(true); click(modal, "Save"); expect(h.writes()).toHaveLength(0);
  });
  it("an empty selector asks for 1–32 values before a mutation is created", async () => {
    const h = harness(); const modal = new FacetQueryEditorModal({} as never, h.client); modal.open(); await waitQuery(modal);
    const name = walk(rootOf(modal)).find((el) => el.tag === "input")!; name.value = "Empty"; name.trigger("input"); click(modal, "Save");
    expect(texts(modal)).toContain("Select 1–32 facet values before saving a query."); expect(h.writes()).toHaveLength(0);
  });
});
