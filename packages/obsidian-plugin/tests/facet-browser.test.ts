import { beforeEach, describe, expect, it, vi } from "vitest";

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
    product_version: "0.1.0", desktop_only: true, state_schema_version: 20, runtime_session_version: 15, operations, facet_capabilities: { version: 1, operations: operations.map((operation) => ({ operation, dto_versions: [1], ...facetLimits(operation) })) } });
  return { client, invoke, writes: () => invoke.mock.calls.filter(([op]) => /\.(create|rename|archive|update|patch|attach)$/.test(op)) };
}
const tick = async (): Promise<void> => { await new Promise((resolve) => setTimeout(resolve, 0)); };

import { FacetBrowserView, FACET_BROWSER_VIEW_TYPE, FacetRecordReadModal } from "../src/facet-browser";
const makeView = (h: ReturnType<typeof harness>, hooks: ConstructorParameters<typeof FacetBrowserView>[2] = {}) => new FacetBrowserView({} as never, h.client, { spaces: [], ...hooks });
const pages = (h: ReturnType<typeof harness>) => h.invoke.mock.calls.filter(([op]) => op === "facet.query.page");

describe("facet browser interaction and lifecycle (UI proof only)", () => {
  it("exports the dockable view and disables facets against an older runtime", async () => {
    const h = harness(() => undefined, []); const view = makeView(h); await view.onOpen();
    expect(view.getViewType()).toBe(FACET_BROWSER_VIEW_TYPE); expect(texts(view).some((text) => text.includes("lacks facet vocabulary support"))).toBe(true); expect(h.invoke).not.toHaveBeenCalled();
  });
  it("distinguishes no vocabulary from no matching records", async () => {
    const h = harness((op) => op === "facet.dimension.list" || op === "facet.query.page" ? page([]) : undefined); const view = makeView(h); await view.onOpen();
    expect(texts(view)).toContain("No vocabulary. Create a dimension and values first."); click(view, "Run selection"); expect(texts(view)).toContain("Select 1–32 facet values before running a query.");
    const populated = harness((op) => op === "facet.query.page" ? page([]) : undefined); const second = makeView(populated); await second.onOpen(); toggle(second, label(value)); click(second, "Run selection"); await tick(); expect(texts(second)).toContain("No matching records.");
  });
  it("ad hoc selectors match every selected value, preserve filters, and fetch exactly 50 without saving", async () => {
    const space = { space_id: id("space", 50), name: "Space" }; const h = harness(); const view = makeView(h, { spaces: [space] }); await view.onOpen();
    toggle(view, label(value)); toggle(view, label(value2)); toggle(view, "canonical"); toggle(view, "reference_or_file"); toggle(view, space.name); click(view, "Run selection");
    await vi.waitFor(() => expect(pages(h)).toHaveLength(1));
    expect(pages(h)[0]![1]).toMatchObject({ limit: 50, cursor: null, selector: { all_value_ids: [value.value_id, value2.value_id], record_types: ["canonical"], payload_families: ["reference_or_file"], space_ids: [space.space_id] } });
    expect(h.writes()).toHaveLength(0); expect(texts(view)).toContain("Match all selected values");
  });
  it("does not prefetch and freezes the running selector during continuation", async () => {
    const h = harness((op, args) => op === "facet.query.page" ? page([result(args.cursor ? 2 : 1)], args.cursor ? null : "cursor-one") : undefined);
    const view = makeView(h); await view.onOpen(); toggle(view, label(value)); click(view, "Run selection"); await tick();
    expect(pages(h)).toHaveLength(1); toggle(view, label(value2)); click(view, "Load more"); await tick();
    expect(pages(h)).toHaveLength(2); expect(pages(h)[1]![1]).toMatchObject({ cursor: "cursor-one", selector: { all_value_ids: [value.value_id] } });
    expect(texts(view)).toContain("2 records loaded. This is not the total matching count.");
    click(view, "Run selection"); await tick(); expect(pages(h)[2]![1]).toMatchObject({ cursor: null, selector: { all_value_ids: [value.value_id, value2.value_id] } });
  });
  it("caps summaries at 500 and requires narrowing or restart", async () => {
    let n = 0; const h = harness((op) => op === "facet.query.page" ? page(Array.from({ length: 50 }, () => result(++n)), `cursor-${n}`) : undefined);
    const view = makeView(h); await view.onOpen(); toggle(view, label(value)); click(view, "Run selection"); await tick();
    for (let i = 1; i < 10; i++) { click(view, "Load more"); await tick(); }
    expect(pages(h)).toHaveLength(10); expect(texts(view)).toContain("500 records loaded. Narrow the selection or restart results.");
    expect(texts(view)).not.toContain("Load more"); expect(texts(view).filter((text) => text.startsWith("Title "))).toHaveLength(500);
  });
  it("keeps stale results until explicit restart and fetches fresh saved-query versions", async () => {
    let pageCount = 0;
    const h = harness((op, args) => {
      if (op === "facet.query.get") return { dto_version: 1, status: "ok", item: { ...query, version: 9 } };
      if (op === "facet.query.page") { pageCount++; if (args.cursor) return Promise.reject({ code: "cursor_stale" }); return page([result(pageCount)], "cursor-first"); }
      return undefined;
    });
    const view = makeView(h); await view.onOpen(); view.showSection("saved"); await tick(); click(view, "Run query"); await tick();
    expect(pages(h)[0]![1]).toMatchObject({ query_id: query.query_id, expected_query_version: 2 }); click(view, "Load more"); await tick();
    expect(texts(view)).toContain("Title 1 <script>"); expect(texts(view)).toContain("Restart results");
    click(view, "Restart results"); await tick(); expect(pages(h)[2]![1]).toMatchObject({ expected_query_version: 9, cursor: null });
    expect(texts(view)).not.toContain("Title 1 <script>"); expect(texts(view)).toContain("Title 3 <script>");
  });
  it("ignores superseded page responses and cannot render after close", async () => {
    let release!: (value: unknown) => void; const h = harness((op) => op === "facet.query.page" ? new Promise((resolve) => { release = resolve; }) : undefined);
    const view = makeView(h); await view.onOpen(); toggle(view, label(value)); click(view, "Run selection"); view.showSection("saved"); await tick();
    release(page([result(1)])); await tick(); expect(texts(view)).not.toContain("Title 1 <script>");
    view.showSection("browse"); click(view, "Run selection"); await view.onClose(); release(page([result(2)])); await tick(); expect(rootOf(view).children).toEqual([]);
  });
  it("renders result classify/read/history actions safely without needing a managed note", async () => {
    const h = harness(); const view = makeView(h); await view.onOpen(); toggle(view, label(value)); click(view, "Run selection"); await tick();
    expect(texts(view)).toContain("Classify"); expect(texts(view)).toContain("Read"); expect(texts(view)).toContain("History"); expect(texts(view)).not.toContain("Open note");
    expect(walk(rootOf(view)).some((el) => el.tag === "script")).toBe(false);
    const obsidian = await import("obsidian") as unknown as { __opened: Array<{ contentEl: HTMLElement }> }; obsidian.__opened.length = 0;
    click(view, "Classify"); await tick(); expect(h.invoke).toHaveBeenCalledWith("facet.assignment.get", expect.objectContaining({ record_id: result(1).record.record_id }));
    expect(obsidian.__opened).toHaveLength(1);
  });
  it("publication receives a freshly read saved query and duplicate starts are disabled", async () => {
    let release!: (value: unknown) => void; const publishQuery = vi.fn();
    const h = harness((op) => op === "facet.query.get" ? new Promise((resolve) => { release = resolve; }) : undefined);
    const view = makeView(h, { publishQuery }); await view.onOpen(); view.showSection("saved"); await tick(); click(view, "New page"); click(view, "New page");
    expect(h.invoke.mock.calls.filter(([op]) => op === "facet.query.get")).toHaveLength(1);
    release({ dto_version: 1, status: "ok", item: { ...query, version: 5 } }); await tick(); expect(publishQuery).toHaveBeenCalledWith({ ...query, version: 5 });
  });
  it("read reuses the bounded record projection and close stops continuation without killing the bridge", async () => {
    let release!: (value: unknown) => void;
    const h = harness((op) => op === "record.read" ? new Promise((resolve) => { release = resolve; }) : undefined);
    const modal = new FacetRecordReadModal({} as never, h.client, result(1)); modal.open(); modal.close();
    release({ dto_version: 1, status: "ok", record: result(1).record, content: { kind: "untrusted_text", text: "x" }, start_byte: 0, end_byte: 1, complete: false, next_cursor: "more" });
    await tick(); expect(h.invoke.mock.calls.filter(([op]) => op === "record.read")).toHaveLength(1); expect(rootOf(modal).children).toEqual([]); expect(h.client.supports("facet.query.page")).toBe(true);
  });
});

describe("facet browser page callbacks", () => {
  it("updates canonical pages using their attached query while keeping classification separate", async () => {
    const pageId = id("page", 60), revisionId = id("revision", 61);
    const canonical: Wire_facet_result = { ...result(1), subject: { kind: "canonical", id: pageId }, record: { ...result(1).record,
      record_id: pageId, revision_id: revisionId, record_type: "canonical", source_id: null } };
    const h = harness((op) => op === "facet.query.page" ? page([canonical]) : op === "facet.query.attachment.get" ? { dto_version: 1, status: "ok", item: { page_id: pageId, query_id: query.query_id, version: 3 } } : undefined);
    const publishQuery = vi.fn(); const view = makeView(h, { publishQuery }); await view.onOpen(); toggle(view, label(value)); click(view, "Run selection"); await tick(); click(view, "Update from saved query"); await tick();
    expect(publishQuery).toHaveBeenCalledWith(query, { pageId, revisionId }); expect(h.writes()).toHaveLength(0);
    expect(texts(view).some((text) => text.includes("classification version 1"))).toBe(true);
  });
  it("a late query read cannot start publication after view disposal", async () => {
    let release!: (value: unknown) => void; const publishQuery = vi.fn();
    const h = harness((op) => op === "facet.query.get" ? new Promise((resolve) => { release = resolve; }) : undefined);
    const view = makeView(h, { publishQuery }); await view.onOpen(); view.showSection("saved"); await tick(); click(view, "New page"); await view.onClose();
    release({ dto_version: 1, status: "ok", item: query }); await tick(); expect(publishQuery).not.toHaveBeenCalled(); expect(rootOf(view).children).toEqual([]);
  });
  it("opens notes only through the coordinator's separately verified mapping callback", async () => {
    const openVerifiedNote = vi.fn(); const h = harness(); const view = makeView(h, { openVerifiedNote, canOpenNote: () => true }); await view.onOpen(); toggle(view, label(value)); click(view, "Run selection"); await tick(); click(view, "Open note");
    expect(openVerifiedNote).toHaveBeenCalledWith(result(1)); expect(h.writes()).toHaveLength(0);
  });
});

it("opens existing dimension and value indexes using validated IDs through the coordinator hook", async () => {
  const h = harness(); const openIndex = vi.fn(); const view = makeView(h, { openIndex }); await view.onOpen(); view.showSection("vocabulary");
  click(view, "Open dimension index"); expect(openIndex).toHaveBeenCalledWith(dimension.dimension_id, undefined);
  click(view, "Open value index"); expect(openIndex).toHaveBeenCalledWith(dimension.dimension_id, value.value_id); expect(h.writes()).toHaveLength(0);
});
