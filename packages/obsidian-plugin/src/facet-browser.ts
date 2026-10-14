import { ItemView, type App, type WorkspaceLeaf } from "obsidian";
import { FacetClient } from "./facet-client";
import type { Wire_facet_result, Wire_saved_query, Wire_selector } from "./facet-types";
import { readCompleteRecord, listSpaces } from "./t07-client";
import { emptyFacetSelector, facetButton, facetError, facetStatus, loadFacetVocabulary, renderFacetSelector,
  openFacetAssignment, openFacetVocabulary, openFacetQueryEditor, openClassificationHistory, facetValueLabel,
  FacetDialog, type FacetVocabulary, type FacetSpace, type FacetEditorHooks } from "./facet-editor";

export const FACET_BROWSER_VIEW_TYPE = "open-brain-facet-browser";
export type FacetBrowserSection = "browse" | "saved" | "vocabulary";
export interface FacetBrowserHooks extends FacetEditorHooks {
  listSpaces?: () => Promise<readonly FacetSpace[]>;
  publishQuery?: (query: Wire_saved_query, target?: { pageId: string; revisionId: string }) => void | Promise<void>;
  /** The coordinator verifies managed custody and waits for Obsidian registration. */
  openVerifiedNote?: (result: Wire_facet_result) => void | Promise<void>;
  canOpenNote?: (result: Wire_facet_result) => boolean;
  /** Existing generated indexes are opened separately from record resolution. */
  openIndex?: (dimensionId: string, valueId?: string) => void | Promise<void>;
}

export class FacetRecordReadModal extends FacetDialog {
  private content = "";
  public constructor(app: App, client: FacetClient, private readonly result: Wire_facet_result) { super(app, client); }
  public override onOpen(): void { super.onOpen(); this.titleEl.setText(this.result.record.title); void this.load(); }
  protected override render(): void {
    if (this.disposed) return;
    this.contentEl.empty(); facetStatus(this.contentEl, this.message);
    if (this.content) this.contentEl.createEl("pre", { text: this.content, cls: "open-brain-record-content" });
    facetButton(this.contentEl, "Close", () => this.close());
  }
  private async load(): Promise<void> {
    const generation = ++this.generation; this.message = "Loading record…"; this.render();
    try {
      const content = await readCompleteRecord(this.client, this.result.record.record_id, this.result.record.revision_id, () => !this.alive(generation));
      if (this.alive(generation)) { this.content = content; this.message = "Read-only record projection."; }
    } catch (error) { if (this.alive(generation)) this.message = facetError(error); }
    if (this.alive(generation)) this.render();
  }
}

/** Foreground-only pagination; no cursor or draft enters plugin settings. */
export class FacetBrowserView extends ItemView {
  private disposed = false;
  private generation = 0;
  private section: FacetBrowserSection = "browse";
  private vocabulary: FacetVocabulary | null = null;
  private spaces: readonly FacetSpace[] = [];
  private selector: Wire_selector = emptyFacetSelector();
  private activeSelector: Wire_selector = emptyFacetSelector();
  private actionBusy = false;
  private selectorDraft: (() => Wire_selector) | null = null;
  private query: Wire_saved_query | null = null;
  private results: Wire_facet_result[] = [];
  private cursor: string | null = null;
  private stale = false;
  private loading = false;
  private initialized = false;
  private pageStarted = false;
  private message = "";
  private queries: Wire_saved_query[] = [];
  private queryCursor: string | null = null;
  private queryLoading = false;
  public constructor(leaf: WorkspaceLeaf, private readonly client: FacetClient, private readonly hooks: FacetBrowserHooks = {}) { super(leaf); }
  public override getViewType(): string { return FACET_BROWSER_VIEW_TYPE; }
  public override getDisplayText(): string { return "Open Brain facets"; }
  public override getIcon(): string { return "tags"; }
  public override async onOpen(): Promise<void> { this.disposed = false; await this.initialize(); }
  public override async onClose(): Promise<void> { this.disposed = true; this.generation += 1; this.contentEl.empty(); }
  public showSection(section: FacetBrowserSection): void {
    if (this.disposed) return;
    if (this.selectorDraft) this.selector = this.selectorDraft();
    this.selectorDraft = null; this.section = section;
    this.generation += 1; this.loading = false; this.queryLoading = false;
    this.render();
    if (section === "saved") void this.loadQueries(true);
    else if (!this.initialized) void this.initialize();
  }
  private alive(generation: number): boolean { return !this.disposed && generation === this.generation; }
  private async initialize(): Promise<void> {
    const generation = ++this.generation; this.message = "Loading vocabulary…"; this.render();
    if (!this.client.supports("facet.dimension.list") || !this.client.supports("facet.value.list")) {
      this.message = "This runtime lacks facet vocabulary support. Upgrade the runtime or check your permissions."; this.render(); return;
    }
    try {
      const vocabulary = await loadFacetVocabulary(this.client, () => !this.alive(generation));
      if (!this.alive(generation)) return;
      const spaces = this.hooks.listSpaces ? await this.hooks.listSpaces() : (this.hooks.spaces ?? await listSpaces(this.client));
      if (!this.alive(generation)) return;
      this.vocabulary = vocabulary; this.spaces = spaces; this.initialized = true; this.message = "";
    } catch (error) { if (this.alive(generation)) this.message = facetError(error); }
    if (this.alive(generation)) this.render();
  }
  private render(): void {
    if (this.disposed) return;
    if (this.selectorDraft) this.selector = this.selectorDraft();
    this.selectorDraft = null;
    this.contentEl.empty(); this.contentEl.addClass("open-brain-facet-browser");
    const navigation = this.contentEl.createEl("nav", { attr: { "aria-label": "Facet browser sections" } });
    for (const [section, label] of [["browse", "Browse"], ["saved", "Saved queries"], ["vocabulary", "Vocabulary"]] as const) {
      const button = facetButton(navigation, label, () => this.showSection(section));
      button.setAttribute("aria-current", this.section === section ? "page" : "false");
    }
    if (this.message) facetStatus(this.contentEl, this.message);
    if (this.section === "vocabulary") {
      facetStatus(this.contentEl, this.vocabulary?.dimensions.length ? "Manage your dimensions and their values." : "No vocabulary. Create a dimension first.");
      facetButton(this.contentEl, "Manage facet vocabulary", () => { openFacetVocabulary(this.app, this.client, () => { if (!this.disposed) void this.initialize(); }); }, !this.client.supports("facet.dimension.list"));
      for (const dimension of this.vocabulary?.dimensions ?? []) {
        const row = this.contentEl.createDiv(); row.createEl("h3", { text: `${dimension.name}${dimension.archived ? " (archived)" : ""}` });
        if (this.hooks.openIndex) facetButton(row, "Open dimension index", () => this.openIndex(dimension.dimension_id));
        for (const value of this.vocabulary?.values.filter((item) => item.dimension_id === dimension.dimension_id) ?? []) {
          const valueRow = row.createDiv(); valueRow.createSpan({ text: facetValueLabel(value.value_id, this.vocabulary!) });
          if (this.hooks.openIndex) facetButton(valueRow, "Open value index", () => this.openIndex(dimension.dimension_id, value.value_id));
        }
      }
      return;
    }
    if (this.section === "saved") { this.renderQueries(); return; }
    if (!this.initialized || !this.vocabulary) { facetButton(this.contentEl, "Reload browser", () => this.initialize()); return; }
    if (this.query) {
      facetStatus(this.contentEl, `Saved query: ${this.query.name}, version ${this.query.version}.`);
      facetButton(this.contentEl, "Edit query", () => this.editQuery(this.query!), !this.client.supports("facet.query.update"));
      facetButton(this.contentEl, "New page", () => this.publish(this.query!), this.actionBusy || !this.hooks.publishQuery || this.query.archived || !this.client.supports("review.propose_from_facet_query"));
      facetButton(this.contentEl, "Browse an ad hoc selection", () => { this.query = null; this.restart(false); });
    }
    const fields = this.contentEl.createEl("fieldset"); fields.disabled = this.loading || this.query !== null;
    this.selectorDraft = renderFacetSelector(fields, this.vocabulary, this.spaces, this.selector);
    facetButton(this.contentEl, "Run selection", () => this.restart(false), this.loading || this.query !== null || !this.client.supports("facet.query.page"));
    facetButton(this.contentEl, "Save selection as query", () => {
      const selector = this.selectorDraft?.() ?? this.selector;
      openFacetQueryEditor(this.app, this.client, null, selector, this.editorHooks());
    }, this.loading || !this.client.supports("facet.query.create"));
    facetStatus(this.contentEl, `${this.results.length} records loaded${this.loading ? "; loading…" : ""}. This is not the total matching count.`);
    if (!this.results.length && this.pageStarted && !this.loading && !this.message) facetStatus(this.contentEl, "No matching records.");
    for (const result of this.results) {
      const row = this.contentEl.createDiv({ cls: "open-brain-facet-result" });
      row.createEl("h3", { text: result.record.title }); row.createEl("p", { text: result.record.excerpt });
      row.createEl("p", { text: `${result.record.record_type} · ${result.record.payload_family} · classification version ${result.classification_version}` });
      facetButton(row, "Classify", () => { openFacetAssignment(this.app, this.client, result.record.record_id); }, !this.client.supports("facet.assignment.get") || !this.client.supports("facet.assignment.patch"));
      facetButton(row, "Read", () => { new FacetRecordReadModal(this.app, this.client, result).open(); });
      facetButton(row, "History", () => { openClassificationHistory(this.app, this.client, result.subject); }, !this.client.supports("facet.history.list"));
      if (result.subject.kind === "canonical" && this.hooks.publishQuery) facetButton(row, "Update from saved query", () => this.updatePage(result), this.actionBusy || !this.client.supports("facet.query.attachment.get") || !this.client.supports("facet.query.get") || !this.client.supports("review.propose_from_facet_query"));
      if (this.hooks.openVerifiedNote && this.hooks.canOpenNote?.(result)) facetButton(row, "Open note", () => this.openNote(result));
    }
    if (this.stale) facetButton(this.contentEl, "Restart results", () => this.restart(true), this.loading);
    else if (this.cursor && this.results.length < 500) facetButton(this.contentEl, "Load more", () => this.loadPage(), this.loading);
    if (this.results.length >= 500) {
      facetStatus(this.contentEl, "500 records loaded. Narrow the selection or restart results.");
      facetButton(this.contentEl, "Restart results", () => this.restart(true), this.loading);
    }
  }
  private editorHooks(): FacetEditorHooks {
    return { spaces: this.spaces, getAttachmentTarget: this.hooks.getAttachmentTarget,
      onChanged: () => { this.hooks.onChanged?.(); if (!this.disposed && this.section === "saved") void this.loadQueries(true); } };
  }
  private editQuery(query: Wire_saved_query): void { openFacetQueryEditor(this.app, this.client, query, query.selector, this.editorHooks()); }
  private restart(refreshQuery: boolean): void {
    if (this.loading || this.disposed) return;
    if (this.selectorDraft && !this.query) this.selector = this.selectorDraft();
    this.activeSelector = this.query?.selector ?? this.selector;
    if (this.activeSelector.all_value_ids.length < 1 || this.activeSelector.all_value_ids.length > 32) { this.message = "Select 1–32 facet values before running a query."; this.render(); return; }
    this.selectorDraft = null; this.results = []; this.cursor = null; this.stale = false; this.message = ""; this.pageStarted = true;
    this.generation += 1;
    void this.loadPage(refreshQuery);
  }
  private async loadPage(refreshQuery = false): Promise<void> {
    if (this.loading || this.stale || this.disposed || this.results.length >= 500) return;
    const generation = this.generation; this.loading = true; this.message = "Loading results…"; this.render();
    try {
      if (refreshQuery && this.query) {
        const query = await this.client.call("facet.query.get", { dto_version: 1, query_id: this.query.query_id });
        if (!this.alive(generation)) return;
        this.query = query.item; this.selector = query.item.selector; this.selectorDraft = null;
      }
      const request = this.query ? { dto_version: 1 as const, query_id: this.query.query_id, expected_query_version: this.query.version, limit: 50, cursor: this.cursor }
        : { dto_version: 1 as const, selector: this.activeSelector, limit: 50, cursor: this.cursor };
      const page = await this.client.call("facet.query.page", request);
      if (!this.alive(generation)) return;
      const seen = new Set(this.results.map((result) => result.record.record_id));
      if (page.items.some((result) => seen.has(result.record.record_id)) || (page.next_cursor !== null && page.next_cursor === this.cursor)) throw { code: "cursor_invalid" };
      this.results.push(...page.items); this.results = this.results.slice(0, 500); this.cursor = page.next_cursor; this.message = "";
    } catch (error) {
      if (!this.alive(generation)) return;
      const code = typeof error === "object" && error !== null && "code" in error ? String(error.code) : "";
      if (["cursor_stale", "version_conflict"].includes(code)) this.stale = true;
      this.message = facetError(error);
    } finally { if (this.alive(generation)) { this.loading = false; this.render(); } }
  }
  private renderQueries(): void {
    if (!this.client.supports("facet.query.list")) { facetStatus(this.contentEl, "This runtime lacks saved query support."); return; }
    facetButton(this.contentEl, "New saved query", () => { openFacetQueryEditor(this.app, this.client, null, emptyFacetSelector(), this.editorHooks()); }, !this.client.supports("facet.query.create"));
    if (!this.queries.length && !this.queryLoading) facetStatus(this.contentEl, "No saved queries.");
    for (const query of this.queries) {
      const row = this.contentEl.createDiv(); row.createEl("h3", { text: `${query.name}${query.archived ? " (archived)" : ""}` });
      row.createEl("p", { text: `Match all selected values · query version ${query.version}` });
      facetButton(row, "Run query", () => { this.query = query; this.selector = query.selector; this.showSection("browse"); this.restart(false); }, query.archived || !this.client.supports("facet.query.page"));
      facetButton(row, "Edit query", () => this.editQuery(query), !this.client.supports("facet.query.update"));
      facetButton(row, "New page", () => this.publish(query), this.actionBusy || query.archived || !this.hooks.publishQuery || !this.client.supports("review.propose_from_facet_query"));
      facetButton(row, "Classification history", () => { openClassificationHistory(this.app, this.client, { kind: "query", id: query.query_id }); }, !this.client.supports("facet.history.list"));
    }
    if (this.queryCursor && this.queries.length < 500) facetButton(this.contentEl, "Load more queries", () => this.loadQueries(false), this.queryLoading);
    if (this.queries.length >= 500) facetStatus(this.contentEl, "500 queries loaded. Reload to restart.");
    facetButton(this.contentEl, "Reload queries", () => this.loadQueries(true), this.queryLoading);
  }
  private async loadQueries(restart: boolean): Promise<void> {
    if (this.queryLoading || this.disposed || !this.client.supports("facet.query.list")) return;
    if (restart) { this.queries = []; this.queryCursor = null; }
    const generation = this.generation; this.queryLoading = true; this.message = "Loading saved queries…"; this.render();
    try {
      const page = await this.client.call("facet.query.list", { dto_version: 1, limit: 50, cursor: this.queryCursor, include_archived: true });
      if (!this.alive(generation)) return;
      this.queries.push(...page.items); this.queries = this.queries.slice(0, 500); this.queryCursor = page.next_cursor; this.message = "";
    } catch (error) { if (this.alive(generation)) this.message = facetError(error); }
    finally { if (this.alive(generation)) { this.queryLoading = false; this.render(); } }
  }
  private async publish(query: Wire_saved_query): Promise<void> {
    if (this.actionBusy || this.disposed) return;
    this.actionBusy = true; this.render();
    const generation = this.generation;
    try {
      const fresh = await this.client.call("facet.query.get", { dto_version: 1, query_id: query.query_id });
      if (this.alive(generation) && !fresh.item.archived) await this.hooks.publishQuery?.(fresh.item);
    } catch (error) { if (this.alive(generation)) this.message = facetError(error); }
    finally { this.actionBusy = false; if (this.alive(generation)) this.render(); }
  }
  private async updatePage(result: Wire_facet_result): Promise<void> {
    if (this.disposed || this.actionBusy || result.subject.kind !== "canonical") return;
    const generation = this.generation; this.actionBusy = true; this.render();
    try {
      const attachment = await this.client.call("facet.query.attachment.get", { dto_version: 1, page_id: result.subject.id });
      if (!this.alive(generation)) return;
      if (!attachment.item) { this.message = "This page has no attached saved query. Attach one explicitly before updating."; return; }
      const query = await this.client.call("facet.query.get", { dto_version: 1, query_id: attachment.item.query_id });
      if (!this.alive(generation)) return;
      if (query.item.archived) { this.message = "The attached saved query is archived."; return; }
      await this.hooks.publishQuery?.(query.item, { pageId: result.subject.id, revisionId: result.record.revision_id });
    } catch (error) { if (this.alive(generation)) this.message = facetError(error); }
    finally { this.actionBusy = false; if (this.alive(generation)) this.render(); }
  }
  private async openIndex(dimensionId: string, valueId?: string): Promise<void> {
    if (this.disposed) return;
    const generation = this.generation;
    try { await this.hooks.openIndex?.(dimensionId, valueId); }
    catch (error) { if (this.alive(generation)) { this.message = facetError(error); this.render(); } }
  }
  private async openNote(result: Wire_facet_result): Promise<void> {
    const generation = this.generation;
    try { if (this.alive(generation)) await this.hooks.openVerifiedNote?.(result); }
    catch (error) { if (this.alive(generation)) { this.message = facetError(error); this.render(); } }
  }
}
