import { Modal, type App } from "obsidian";
import { FacetClient, type FacetMutation } from "./facet-client";
import type { FacetOperation, FacetRequestMap, Wire_assignment_state, Wire_dimension, Wire_value,
  Wire_saved_query, Wire_selector, Wire_history_subject, Wire_facet_dimension_list_response, Wire_facet_value_list_response } from "./facet-types";

export interface FacetSpace { space_id: string; name: string }
export interface FacetEditorHooks {
  spaces?: readonly FacetSpace[];
  getAttachmentTarget?: () => Promise<{ pageId: string; revisionId: string } | null>;
  onChanged?: () => void;
}
export interface FacetVocabulary { dimensions: Wire_dimension[]; values: Wire_value[] }
export const emptyFacetSelector = (): Wire_selector => ({ all_value_ids: [], space_ids: [], record_types: [], payload_families: [] });
export function facetError(error: unknown): string {
  const code = typeof error === "object" && error !== null && "code" in error ? String(error.code) : "operation_failed";
  const messages: Record<string, string> = {
    unsupported_capability: "This runtime lacks this facet action. Upgrade the runtime or check your permissions.",
    version_conflict: "The state changed while you were editing. Your draft is preserved.",
    revision_changed: "The record content changed. Your draft is preserved.",
    cursor_stale: "Results changed. Restart results to read a fresh selection.",
    name_conflict: "That name is already in use. Choose another name.",
    facet_archived: "An archived facet cannot be added. Reload the vocabulary.",
    query_archived: "This saved query is archived.",
    not_found: "This item is unavailable under the current runtime permissions.",
    limit_exceeded: "The runtime limit was reached. Narrow your selection.",
  };
  return messages[code] ?? `Facet action failed (${code}).`;
}
export function facetButton(parent: HTMLElement, text: string, action: () => void | Promise<void>, disabled = false): HTMLButtonElement {
  const button = parent.createEl("button", { text });
  button.type = "button"; button.disabled = disabled;
  button.addEventListener("click", () => { if (!button.disabled) void action(); });
  return button;
}
export function facetStatus(parent: HTMLElement, text: string): HTMLElement {
  return parent.createEl("p", { text, attr: { role: "status", "aria-live": "polite" } });
}
function codeOf(error: unknown): string {
  return typeof error === "object" && error !== null && "code" in error ? String(error.code) : "operation_failed";
}
export async function loadFacetVocabulary(client: FacetClient, cancelled: () => boolean = () => false): Promise<FacetVocabulary> {
  const dimensions: Wire_dimension[] = [], values: Wire_value[] = [];
  let cursor: string | null = null;
  const seen = new Set<string>();
  do {
    if (cancelled()) throw new Error("disposed");
    const page: Wire_facet_dimension_list_response = await client.call("facet.dimension.list", { dto_version: 1, include_archived: true, limit: 50, cursor });
    if (cancelled()) throw new Error("disposed");
    dimensions.push(...page.items); cursor = page.next_cursor;
    if (dimensions.length > 2_000 || (cursor !== null && seen.has(cursor))) throw { code: "limit_exceeded" };
    if (cursor !== null) seen.add(cursor);
  } while (cursor !== null);
  for (const dimension of dimensions) {
    cursor = null; seen.clear();
    do {
      if (cancelled()) throw new Error("disposed");
      const page: Wire_facet_value_list_response = await client.call("facet.value.list", { dto_version: 1, dimension_id: dimension.dimension_id, include_archived: true, limit: 50, cursor });
      if (cancelled()) throw new Error("disposed");
      values.push(...page.items); cursor = page.next_cursor;
      if (values.length > 10_000 || (cursor !== null && seen.has(cursor))) throw { code: "limit_exceeded" };
      if (cursor !== null) seen.add(cursor);
    } while (cursor !== null);
  }
  return { dimensions, values };
}
export function facetValueLabel(valueId: string, vocabulary: FacetVocabulary): string {
  const value = vocabulary.values.find((item) => item.value_id === valueId);
  if (!value) return `Unavailable value (${valueId})`;
  const dimension = vocabulary.dimensions.find((item) => item.dimension_id === value.dimension_id);
  return `${dimension?.name ?? "Unavailable dimension"}: ${value.name}${value.effective_archived ? " (archived)" : ""}`;
}
function check(parent: HTMLElement, text: string, selected: boolean, change: (checked: boolean) => void, disabled = false): HTMLInputElement {
  const label = parent.createEl("label", { cls: "open-brain-facet-choice" });
  const input = label.createEl("input", { type: "checkbox" }); input.checked = selected; input.disabled = disabled;
  label.createSpan({ text });
  input.addEventListener("change", () => { if (!input.disabled) change(input.checked); });
  return input;
}
export function renderFacetSelector(parent: HTMLElement, vocabulary: FacetVocabulary, spaces: readonly FacetSpace[], initial: Wire_selector): () => Wire_selector {
  const values = new Set(initial.all_value_ids), spaceIds = new Set(initial.space_ids);
  const recordTypes = new Set(initial.record_types), families = new Set(initial.payload_families);
  parent.createEl("p", { text: "Match all selected values" });
  const update = (set: Set<string>, id: string, checked: boolean): void => { if (checked) set.add(id); else set.delete(id); };
  const valueGroup = parent.createEl("fieldset"); valueGroup.createEl("legend", { text: "Facet values" });
  if (!vocabulary.values.length) valueGroup.createEl("p", { text: "No vocabulary. Create a dimension and values first." });
  for (const value of vocabulary.values) check(valueGroup, facetValueLabel(value.value_id, vocabulary), values.has(value.value_id), (checked) => update(values, value.value_id, checked));
  for (const id of values) if (!vocabulary.values.some((v) => v.value_id === id)) check(valueGroup, facetValueLabel(id, vocabulary), true, (checked) => update(values, id, checked));
  const types = parent.createEl("fieldset"); types.createEl("legend", { text: "Record types" });
  for (const type of ["source", "canonical"] as const) check(types, type, recordTypes.has(type), (checked) => update(recordTypes, type, checked));
  const payloads = parent.createEl("fieldset"); payloads.createEl("legend", { text: "Payload families" });
  for (const family of ["text", "event", "measurement", "reference_or_file"] as const) check(payloads, family, families.has(family), (checked) => update(families, family, checked));
  const spaceGroup = parent.createEl("fieldset"); spaceGroup.createEl("legend", { text: "Spaces" });
  for (const space of spaces) check(spaceGroup, space.name, spaceIds.has(space.space_id), (checked) => update(spaceIds, space.space_id, checked));
  for (const id of spaceIds) if (!spaces.some((space) => space.space_id === id)) check(spaceGroup, `Selected space (${id})`, true, (checked) => update(spaceIds, id, checked));
  return () => ({ all_value_ids: [...values].sort(), space_ids: [...spaceIds].sort(), record_types: [...recordTypes].sort(), payload_families: [...families].sort() });
}

/** Closing discards in-memory drafts; it never cancels or rolls back an engine write. */
const activeFacetDialogs = new WeakMap<FacetClient, Set<FacetDialog>>();
export function disposeFacetDialogs(client: FacetClient): void {
  for (const dialog of [...(activeFacetDialogs.get(client) ?? [])]) dialog.dispose();
}

export abstract class FacetDialog extends Modal {
  protected disposed = false;
  protected generation = 0;
  protected busy = false;
  private pendingMutation = false;
  protected uncertain: FacetMutation<FacetOperation> | null = null;
  protected message = "";
  private returnFocus: HTMLElement | null = null;
  private discardConfirmed = false;
  public constructor(app: App, protected readonly client: FacetClient) { super(app); }
  public override onOpen(): void {
    this.disposed = false;
    const dialogs = activeFacetDialogs.get(this.client) ?? new Set<FacetDialog>();
    dialogs.add(this); activeFacetDialogs.set(this.client, dialogs);
    this.returnFocus = typeof document !== "undefined" ? document.activeElement as HTMLElement | null : null;
  }
  public override close(): void {
    if (!this.discardConfirmed && (this.pendingMutation || this.uncertain !== null)) {
      const confirmation = this.contentEl.createDiv();
      facetStatus(confirmation, "This write may still commit. Closing discards its in-memory retry request. Reopen and reload state/history before another write.");
      facetButton(confirmation, "Keep dialog open", () => confirmation.remove());
      facetButton(confirmation, "Discard retry state and close", () => { this.discardConfirmed = true; super.close(); });
      return;
    }
    super.close();
  }
  public dispose(): void { this.discardConfirmed = true; super.close(); }
  public override onClose(): void {
    activeFacetDialogs.get(this.client)?.delete(this);
    this.disposed = true; this.generation += 1; this.contentEl.empty();
    this.returnFocus?.focus();
  }
  protected alive(generation: number): boolean { return !this.disposed && generation === this.generation; }
  protected mutationBlocked(): boolean { return this.busy || this.uncertain !== null; }
  protected renderRetry(parent: HTMLElement, changed: () => void): void {
    if (this.message) facetStatus(parent, this.message);
    if (this.uncertain) facetButton(parent, "Retry same operation", () => this.execute(this.uncertain!, changed), this.busy);
  }
  protected async mutate<O extends FacetOperation>(operation: O, args: Omit<FacetRequestMap[O], "operation_id">, changed: () => void): Promise<void> {
    if (this.disposed || this.mutationBlocked()) return;
    await this.execute(this.client.createMutation(operation, args) as FacetMutation<FacetOperation>, changed);
  }
  protected async execute(mutation: FacetMutation<FacetOperation>, changed: () => void): Promise<void> {
    if (this.disposed || this.busy) return;
    const generation = this.generation; this.busy = true; this.pendingMutation = true; this.message = "Saving… Closing or unloading discards the in-memory retry request; the write may still commit."; this.render();
    try {
      await mutation.submit();
      if (!this.alive(generation)) return;
      this.uncertain = null; this.busy = false; this.pendingMutation = false; this.message = "Saved."; changed();
    } catch (error) {
      if (!this.alive(generation)) return;
      if (mutation.status === "uncertain") { this.uncertain = mutation; this.message = "The result is uncertain. Retry the same operation; do not submit a replacement."; }
      else { this.uncertain = null; this.message = facetError(error); await this.conflict(error); }
    } finally { if (this.alive(generation)) { this.busy = false; this.pendingMutation = false; this.render(); } }
  }
  protected async conflict(_error: unknown): Promise<void> {}
  protected abstract render(): void;
}

export class FacetAssignmentModal extends FacetDialog {
  private state: Wire_assignment_state | null = null;
  private current: Wire_assignment_state | null = null;
  private vocabulary: FacetVocabulary = { dimensions: [], values: [] };
  private draft = new Set<string>();
  private additions = new Set<string>();
  private removals = new Set<string>();
  private stale = false;
  public constructor(app: App, client: FacetClient, private readonly recordId: string, private readonly onChanged: () => void = () => {}) { super(app, client); }
  public override onOpen(): void { super.onOpen(); this.titleEl.setText("Classify record"); void this.load(); }
  private async load(): Promise<void> {
    const generation = ++this.generation; this.message = "Loading classification…"; this.render();
    try {
      const state = await this.client.call("facet.assignment.get", { dto_version: 1, record_id: this.recordId });
      if (!this.alive(generation)) return;
      const vocabulary = await loadFacetVocabulary(this.client, () => !this.alive(generation));
      if (!this.alive(generation)) return;
      this.state = state.item; this.stale = false; this.current = null; this.vocabulary = vocabulary; this.draft = new Set(state.item.memberships.map((m) => m.value_id)); this.message = "";
    } catch (error) { if (this.alive(generation)) this.message = facetError(error); }
    if (this.alive(generation)) this.render();
  }
  protected override render(): void {
    if (this.disposed) return;
    this.contentEl.empty(); this.renderRetry(this.contentEl, () => { this.onChanged(); void this.load(); });
    if (!this.state) { facetButton(this.contentEl, "Reload", () => this.load()); return; }
    facetStatus(this.contentEl, `Classification version ${this.state.version}. Save sends one assignment patch. Cancel has no effect.`);
    const existing = new Set(this.state.memberships.map((m) => m.value_id));
    const ids = new Set([...existing, ...this.vocabulary.values.map((v) => v.value_id)]);
    for (const id of ids) {
      const value = this.vocabulary.values.find((v) => v.value_id === id);
      const archived = !value || value.effective_archived;
      const input = check(this.contentEl, facetValueLabel(id, this.vocabulary), this.draft.has(id), (checked) => {
        if (checked && archived && !existing.has(id)) { input.checked = false; return; }
        if (checked) this.draft.add(id); else this.draft.delete(id);
        if (archived && !existing.has(id) && !checked) input.disabled = true;
      }, this.mutationBlocked() || (archived && !existing.has(id) && !this.draft.has(id)));
    }
    if (this.current) {
      const conflict = this.contentEl.createDiv();
      facetStatus(conflict, `Current classification version ${this.current.version}; revision ${this.current.revision_id}. Requested changes remain in your draft.`);
      conflict.createEl("p", { text: `Current: ${this.current.memberships.map((m) => facetValueLabel(m.value_id, this.vocabulary)).join(", ") || "No values"}` });
      conflict.createEl("p", { text: `Requested additions: ${[...this.additions].map((id) => facetValueLabel(id, this.vocabulary)).join(", ") || "None"}` });
      conflict.createEl("p", { text: `Requested removals: ${[...this.removals].map((id) => facetValueLabel(id, this.vocabulary)).join(", ") || "None"}` });
      facetButton(conflict, "Reload and reapply draft", () => this.reapply(), this.mutationBlocked());
    }
    facetButton(this.contentEl, "Save", () => this.save(), this.mutationBlocked() || this.stale || !this.client.supports("facet.assignment.patch"));
    facetButton(this.contentEl, "Cancel", () => this.close());
  }
  private async save(): Promise<void> {
    if (!this.state || this.stale || this.mutationBlocked()) return;
    if (this.draft.size > 128) { this.message = "At most 128 memberships can be saved. Remove values first."; this.render(); return; }
    const original = new Set(this.state.memberships.map((m) => m.value_id));
    this.additions = new Set([...this.draft].filter((id) => !original.has(id)));
    this.removals = new Set([...original].filter((id) => !this.draft.has(id)));
    await this.mutate("facet.assignment.patch", { dto_version: 1, record_id: this.state.record_id, expected_revision_id: this.state.revision_id, expected_version: this.state.version,
      add_value_ids: [...this.additions].sort(), remove_value_ids: [...this.removals].sort() }, () => { this.onChanged(); void this.load(); });
  }
  protected override async conflict(error: unknown): Promise<void> {
    if (!["version_conflict", "revision_changed"].includes(codeOf(error))) return;
    this.stale = true;
    const generation = this.generation;
    try { const current = await this.client.call("facet.assignment.get", { dto_version: 1, record_id: this.recordId }); if (this.alive(generation)) this.current = current.item; }
    catch { if (this.alive(generation)) this.message += " Current state is unavailable; reopen before replacing this write."; }
  }
  private async reapply(): Promise<void> {
    if (!this.current || this.mutationBlocked()) return;
    const original = new Set(this.state?.memberships.map((m) => m.value_id) ?? []);
    this.additions = new Set([...this.draft].filter((id) => !original.has(id)));
    this.removals = new Set([...original].filter((id) => !this.draft.has(id)));
    const generation = ++this.generation;
    try {
      const fresh = await this.client.call("facet.assignment.get", { dto_version: 1, record_id: this.recordId });
      if (!this.alive(generation)) return;
      const vocabulary = await loadFacetVocabulary(this.client, () => !this.alive(generation));
      if (!this.alive(generation)) return;
      this.state = fresh.item; this.vocabulary = vocabulary;
      this.draft = new Set(fresh.item.memberships.map((m) => m.value_id));
      for (const id of this.additions) this.draft.add(id);
      for (const id of this.removals) this.draft.delete(id);
      this.current = null; this.stale = false; this.message = "Draft reapplied to fresh versions. Review it and select Save again.";
    } catch (error) { if (this.alive(generation)) this.message = facetError(error); }
    if (this.alive(generation)) this.render();
  }
}

type VocabularyAction = { kind: "dimension" | "value"; action: "create" | "rename" | "archive"; dimension?: Wire_dimension; value?: Wire_value };
export class FacetVocabularyEditModal extends FacetDialog {
  private name = "";
  private confirmed = false;
  public constructor(app: App, client: FacetClient, private readonly edit: VocabularyAction, private readonly onChanged: () => void) {
    super(app, client); this.name = edit.value?.name ?? (edit.action === "rename" ? edit.dimension?.name ?? "" : "");
  }
  public override onOpen(): void { super.onOpen(); this.titleEl.setText(`${this.edit.action} ${this.edit.kind}`); this.render(); }
  protected override render(): void {
    if (this.disposed) return;
    this.contentEl.empty(); this.renderRetry(this.contentEl, () => { this.confirmed = true; this.onChanged(); });
    if (this.edit.action === "archive") facetStatus(this.contentEl, "Archive permanently? Existing assignments and queries remain. New assignments are blocked. Stage 1 has no unarchive.");
    else {
      const label = this.contentEl.createEl("label", { text: "Name" });
      const input = label.createEl("input", { type: "text" }); input.value = this.name; input.disabled = this.mutationBlocked() || this.confirmed;
      input.addEventListener("input", () => { this.name = input.value; });
    }
    facetButton(this.contentEl, this.edit.action === "archive" ? "Confirm archive" : "Save", () => this.save(), this.mutationBlocked() || this.confirmed);
    facetButton(this.contentEl, "Cancel", () => this.close());
  }
  private async save(): Promise<void> {
    const e = this.edit;
    if (this.confirmed) return;
    const done = (): void => { this.confirmed = true; this.onChanged(); this.message = "Saved. Close this dialog to return."; };
    if (e.action !== "archive" && !this.name.trim()) { this.message = "Enter a name."; this.render(); return; }
    if (e.kind === "dimension") {
      if (e.action === "create") await this.mutate("facet.dimension.create", { dto_version: 1, expected_version: 0, name: this.name }, done);
      else if (e.dimension && e.action === "rename") await this.mutate("facet.dimension.rename", { dto_version: 1, dimension_id: e.dimension.dimension_id, expected_version: e.dimension.version, name: this.name }, done);
      else if (e.dimension) await this.mutate("facet.dimension.archive", { dto_version: 1, dimension_id: e.dimension.dimension_id, expected_version: e.dimension.version }, done);
    } else if (e.action === "create" && e.dimension) await this.mutate("facet.value.create", { dto_version: 1, expected_version: 0, dimension_id: e.dimension.dimension_id, expected_dimension_version: e.dimension.version, name: this.name }, done);
    else if (e.value && e.action === "rename") await this.mutate("facet.value.rename", { dto_version: 1, value_id: e.value.value_id, expected_version: e.value.version, name: this.name }, done);
    else if (e.value) await this.mutate("facet.value.archive", { dto_version: 1, value_id: e.value.value_id, expected_version: e.value.version }, done);
  }
}
export class FacetVocabularyModal extends FacetDialog {
  private vocabulary: FacetVocabulary | null = null;
  public constructor(app: App, client: FacetClient, private readonly onChanged: () => void = () => {}) { super(app, client); }
  public override onOpen(): void { super.onOpen(); this.titleEl.setText("Facet vocabulary"); void this.load(); }
  private async load(): Promise<void> {
    const generation = ++this.generation; this.message = "Loading vocabulary…"; this.render();
    try { const vocabulary = await loadFacetVocabulary(this.client, () => !this.alive(generation)); if (this.alive(generation)) { this.vocabulary = vocabulary; this.message = ""; } }
    catch (error) { if (this.alive(generation)) this.message = facetError(error); }
    if (this.alive(generation)) this.render();
  }
  protected override render(): void {
    if (this.disposed) return;
    this.contentEl.empty(); facetStatus(this.contentEl, this.message || "Vocabulary ready.");
    facetButton(this.contentEl, "Create dimension", () => this.edit({ kind: "dimension", action: "create" }), !this.client.supports("facet.dimension.create"));
    if (this.vocabulary && !this.vocabulary.dimensions.length) facetStatus(this.contentEl, "No vocabulary. Create a dimension first.");
    for (const dimension of this.vocabulary?.dimensions ?? []) {
      const row = this.contentEl.createDiv(); row.createEl("h3", { text: `${dimension.name}${dimension.archived ? " (archived)" : ""}` });
      facetButton(row, "Rename dimension", () => this.edit({ kind: "dimension", action: "rename", dimension }), !this.client.supports("facet.dimension.rename"));
      facetButton(row, "Archive dimension", () => this.edit({ kind: "dimension", action: "archive", dimension }), dimension.archived || !this.client.supports("facet.dimension.archive"));
      facetButton(row, "Create value", () => this.edit({ kind: "value", action: "create", dimension }), dimension.archived || !this.client.supports("facet.value.create"));
      facetButton(row, "Classification history", () => { openClassificationHistory(this.app, this.client, { kind: "dimension", id: dimension.dimension_id }); }, !this.client.supports("facet.history.list"));
      for (const value of this.vocabulary?.values.filter((v) => v.dimension_id === dimension.dimension_id) ?? []) {
        const valueRow = row.createDiv(); valueRow.createSpan({ text: facetValueLabel(value.value_id, this.vocabulary!) });
        facetButton(valueRow, "Rename value", () => this.edit({ kind: "value", action: "rename", value }), !this.client.supports("facet.value.rename"));
        facetButton(valueRow, "Archive value", () => this.edit({ kind: "value", action: "archive", value }), value.archived || !this.client.supports("facet.value.archive"));
        facetButton(valueRow, "Classification history", () => { openClassificationHistory(this.app, this.client, { kind: "value", id: value.value_id }); }, !this.client.supports("facet.history.list"));
      }
    }
    facetButton(this.contentEl, "Reload vocabulary", () => this.load());
    facetButton(this.contentEl, "Close", () => this.close());
  }
  private edit(edit: VocabularyAction): void {
    new FacetVocabularyEditModal(this.app, this.client, edit, () => { this.onChanged(); if (!this.disposed) void this.load(); }).open();
  }
}

export class FacetQueryEditorModal extends FacetDialog {
  private vocabulary: FacetVocabulary | null = null;
  private name: string;
  private selector: Wire_selector;
  private getSelector: (() => Wire_selector) | null = null;
  private freshQuery: Wire_saved_query | null = null;
  private confirmed = false;
  public constructor(app: App, client: FacetClient, private query: Wire_saved_query | null = null, selector = emptyFacetSelector(), private readonly hooks: FacetEditorHooks = {}) {
    super(app, client); this.name = query?.name ?? ""; this.selector = query?.selector ?? selector;
  }
  public override onOpen(): void { super.onOpen(); this.titleEl.setText(this.query ? "Edit saved facet query" : "Save facet query"); void this.load(); }
  private async load(): Promise<void> {
    const generation = ++this.generation; this.message = "Loading vocabulary…"; this.render();
    try { const vocabulary = await loadFacetVocabulary(this.client, () => !this.alive(generation)); if (this.alive(generation)) { this.vocabulary = vocabulary; this.message = ""; } }
    catch (error) { if (this.alive(generation)) this.message = facetError(error); }
    if (this.alive(generation)) this.render();
  }
  protected override render(): void {
    if (this.disposed) return;
    if (this.getSelector) this.selector = this.getSelector();
    this.contentEl.empty(); this.renderRetry(this.contentEl, () => { this.confirmed = true; this.hooks.onChanged?.(); });
    const locked = this.mutationBlocked() || this.confirmed || !!this.query?.archived;
    const label = this.contentEl.createEl("label", { text: "Query name" });
    const name = label.createEl("input", { type: "text" }); name.value = this.name; name.disabled = locked;
    name.addEventListener("input", () => { this.name = name.value; });
    const fields = this.contentEl.createEl("fieldset"); fields.disabled = locked;
    if (this.vocabulary) this.getSelector = renderFacetSelector(fields, this.vocabulary, this.hooks.spaces ?? [], this.selector);
    if (this.query?.archived) facetStatus(this.contentEl, "Archived query. Its selection remains available for inspection.");
    if (this.freshQuery) {
      facetStatus(this.contentEl, `Current query version ${this.freshQuery.version}: ${this.freshQuery.name}. Your requested name and selection are preserved.`);
      this.contentEl.createEl("pre", { text: JSON.stringify(this.freshQuery.selector, null, 2) });
      facetButton(this.contentEl, "Reload versions and keep draft", () => { this.query = this.freshQuery; this.freshQuery = null; this.message = "Fresh versions loaded. Select Save again to reapply your draft."; this.render(); }, locked);
    }
    facetButton(this.contentEl, "Save", () => this.save(), locked || !this.vocabulary || !!this.freshQuery || !this.client.supports(this.query ? "facet.query.update" : "facet.query.create"));
    if (this.query) {
      facetButton(this.contentEl, "Archive query", () => {
        const confirmation = this.contentEl.createDiv();
        facetStatus(confirmation, "Archive this saved query permanently? Existing attachments remain. New attachments and publication are blocked. Stage 1 has no unarchive.");
        facetButton(confirmation, "Confirm archive", () => this.archive(), locked);
        facetButton(confirmation, "Cancel archive", () => confirmation.remove());
      }, locked || !this.client.supports("facet.query.archive"));
      if (this.hooks.getAttachmentTarget) facetButton(this.contentEl, "Attach to current managed page", () => this.prepareAttach(), locked || !this.client.supports("facet.query.attach"));
    }
    facetButton(this.contentEl, "Cancel", () => this.close());
  }
  private async save(): Promise<void> {
    if (!this.vocabulary || !this.name.trim() || this.freshQuery || this.confirmed) { this.message = "Enter a query name and review the selection."; this.render(); return; }
    this.selector = this.getSelector?.() ?? this.selector;
    if (this.selector.all_value_ids.length < 1 || this.selector.all_value_ids.length > 32) { this.message = "Select 1–32 facet values before saving a query."; this.render(); return; }
    const done = (): void => { this.confirmed = true; this.hooks.onChanged?.(); };
    if (this.query) await this.mutate("facet.query.update", { dto_version: 1, query_id: this.query.query_id, expected_version: this.query.version, name: this.name, selector: this.selector }, done);
    else await this.mutate("facet.query.create", { dto_version: 1, expected_version: 0, name: this.name, selector: this.selector }, done);
  }
  private async archive(): Promise<void> {
    if (!this.query) return;
    await this.mutate("facet.query.archive", { dto_version: 1, query_id: this.query.query_id, expected_version: this.query.version }, () => { this.confirmed = true; this.hooks.onChanged?.(); });
  }
  protected override async conflict(error: unknown): Promise<void> {
    if (codeOf(error) !== "version_conflict" || !this.query) return;
    const generation = this.generation;
    try { const fresh = await this.client.call("facet.query.get", { dto_version: 1, query_id: this.query.query_id }); if (this.alive(generation)) this.freshQuery = fresh.item; }
    catch { if (this.alive(generation)) this.message += " Current query is unavailable."; }
  }
  private async prepareAttach(): Promise<void> {
    if (!this.query || !this.hooks.getAttachmentTarget || this.mutationBlocked()) return;
    const generation = this.generation; this.busy = true; this.render();
    try {
      const target = await this.hooks.getAttachmentTarget();
      if (!target || !this.alive(generation)) return;
      const attachment = await this.client.call("facet.query.attachment.get", { dto_version: 1, page_id: target.pageId });
      if (!this.alive(generation)) return;
      const query = await this.client.call("facet.query.get", { dto_version: 1, query_id: this.query.query_id });
      if (!this.alive(generation)) return;
      this.busy = false; this.render();
      const confirmation = this.contentEl.createDiv();
      facetStatus(confirmation, `Attach ${query.item.name} to managed page ${target.pageId}? This changes its source-selection query; it does not assign facets to the page.`);
      facetButton(confirmation, "Confirm attachment", () => this.mutate("facet.query.attach", { dto_version: 1, page_id: target.pageId, expected_revision_id: target.revisionId,
        expected_version: attachment.item?.version ?? 0, query_id: query.item.query_id, expected_query_version: query.item.version }, () => { this.confirmed = true; this.hooks.onChanged?.(); }), query.item.archived);
      facetButton(confirmation, "Cancel attachment", () => confirmation.remove());
    } catch (error) { if (this.alive(generation)) { this.busy = false; this.message = facetError(error); this.render(); } }
    finally { if (this.alive(generation) && this.busy) { this.busy = false; this.render(); } }
  }
}

export class ClassificationHistoryModal extends FacetDialog {
  private cursor: string | null = null;
  private rows: Array<{ sequence: number; text: string }> = [];
  private loading = false;
  public constructor(app: App, client: FacetClient, private readonly subject: Wire_history_subject) { super(app, client); }
  public override onOpen(): void { super.onOpen(); this.titleEl.setText("Classification history"); void this.load(); }
  protected override render(): void {
    if (this.disposed) return;
    this.contentEl.empty(); facetStatus(this.contentEl, this.message || (this.rows.length ? "Read-only classification events; content revisions and restoring memberships are separate." : "No classification history."));
    for (const row of this.rows) this.contentEl.createEl("pre", { text: row.text, cls: "open-brain-facet-evidence" });
    if (this.cursor && this.rows.length < 500) facetButton(this.contentEl, "Load more history", () => this.load(), this.loading);
    if (this.rows.length >= 500) facetStatus(this.contentEl, "500 history events loaded. Close and reopen to restart.");
    facetButton(this.contentEl, "Close", () => this.close());
  }
  private async load(): Promise<void> {
    if (this.loading || this.disposed) return;
    const generation = this.generation; this.loading = true; this.message = "Loading classification history…"; this.render();
    try {
      const page = await this.client.call("facet.history.list", { dto_version: 1, subject: this.subject, cursor: this.cursor, limit: 50 });
      if (!this.alive(generation)) return;
      for (const item of page.items) this.rows.push({ sequence: item.sequence, text: `Event ${item.sequence} · ${item.recorded_at} · ${item.kind}\n${JSON.stringify(item.changes, null, 2)}` });
      this.rows = this.rows.slice(0, 500); this.cursor = page.next_cursor; this.message = "";
    } catch (error) { if (this.alive(generation)) this.message = facetError(error); }
    finally { if (this.alive(generation)) { this.loading = false; this.render(); } }
  }
}

export function openFacetAssignment(app: App, client: FacetClient, recordId: string, onChanged?: () => void): FacetAssignmentModal {
  const modal = new FacetAssignmentModal(app, client, recordId, onChanged); modal.open(); return modal;
}
export function openFacetVocabulary(app: App, client: FacetClient, onChanged?: () => void): FacetVocabularyModal {
  const modal = new FacetVocabularyModal(app, client, onChanged); modal.open(); return modal;
}
export function openFacetQueryEditor(app: App, client: FacetClient, query: Wire_saved_query | null = null, selector = emptyFacetSelector(), hooks: FacetEditorHooks = {}): FacetQueryEditorModal {
  const modal = new FacetQueryEditorModal(app, client, query, selector, hooks); modal.open(); return modal;
}
export function openClassificationHistory(app: App, client: FacetClient, subject: Wire_history_subject): ClassificationHistoryModal {
  const modal = new ClassificationHistoryModal(app, client, subject); modal.open(); return modal;
}
