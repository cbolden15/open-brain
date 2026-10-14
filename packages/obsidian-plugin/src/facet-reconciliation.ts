import { Modal, type App } from "obsidian";
import { FacetClient, type FacetMutation } from "./facet-client";
import type { Wire_facet_vault_preview_response, Wire_facet_vault_apply_response, Wire_history_entry, Wire_vault_conflict } from "./facet-types";

export interface FacetReconciliationOptions {
  workspaceId: string;
  recordId: string;
  refreshManaged: () => Promise<void>;
  editNote?: () => Promise<void>;
  returnFocus?: HTMLElement;
}

const conflicts: Record<Wire_vault_conflict["code"], string> = {
  unknown_name: "A facet label is unknown. Edit the note to use existing vocabulary.",
  ambiguous_name: "A facet label matches more than one value. Edit the note to identify the intended value.",
  archived_addition: "An archived value cannot be added. Remove that addition from the note.",
  content_changed: "The note body changed. Review body edits through the existing content workflow, then refresh this preview.",
  projection_changed: "The managed projection changed. Refresh the preview; edited generated indexes must be preserved.",
  invalid_frontmatter: "The frontmatter is invalid. Edit the note, then refresh the preview.",
};
const discardedWrites = new WeakMap<FacetClient, Set<string>>();
const errorCode = (error: unknown): string => typeof error === "object" && error !== null && "code" in error ? String(error.code) : "transport_failed";
function failure(error: unknown): string {
  switch (errorCode(error)) {
    case "preview_stale": return "This preview expired or the note or classification changed. Request a fresh preview and confirm it again.";
    case "projection_conflict": return "The managed projection changed. Preserve your edits and request a fresh preview.";
    case "revision_changed": return "The note body changed. Review body edits through the content workflow and request a fresh preview.";
    case "version_conflict": return "Classification changed since this preview. Request a fresh preview and confirm it again.";
    case "not_found": return "The managed record is unavailable. Refresh or observe the managed workspace before trying again.";
    case "unsupported_capability": return "This runtime does not support this facet action. Upgrade the runtime.";
    default: return "The operation failed. Your note edits are preserved.";
  }
}

/** A foreground review of engine-owned reconciliation, with session-only retry state. */
export class FacetReconciliationModal extends Modal {
  #closed = false;
  #epoch = 0;
  #renderVersion = 0;
  #busy = false;
  #confirmed = false;
  #freshRequired = false;
  #discardConfirmation = false;
  #preview: Wire_facet_vault_preview_response | null = null;
  #mutation: FacetMutation<"facet.vault.apply"> | null = null;
  #receipt: Wire_facet_vault_apply_response | null = null;
  #message = "Loading reconciliation preview…";
  #labels = new Map<string, string>();
  #labelMessage = "";
  #history: Wire_history_entry[] = [];
  #historyCursor: string | null = null;
  #historyLoaded = false;
  #historyBusy = false;
  #historyMessage = "";
  #refreshBusy = false;
  #timer: ReturnType<typeof setTimeout> | null = null;
  #focus: HTMLElement | null;
  public constructor(app: App, private readonly client: FacetClient, private readonly options: FacetReconciliationOptions) {
    super(app);
    this.#focus = options.returnFocus ?? (this.contentEl.ownerDocument.activeElement as HTMLElement | null);
  }
  public override onOpen(): void {
    this.titleEl.setText("Review Markdown facet edits");
    void this.refreshPreview();
  }
  public override close(): void {
    if (!this.#closed && this.unresolved() && !this.#discardConfirmation) {
      this.#discardConfirmation = true;
      this.#message = "The write may already have committed. Closing discards this dialog's retry state; cancellation is not rollback. Reload classification and history before a replacement action.";
      this.render();
      return;
    }
    super.close();
  }
  public override onClose(): void {
    if (this.unresolved()) {
      const records = discardedWrites.get(this.client) ?? new Set<string>();
      records.add(this.options.recordId); discardedWrites.set(this.client, records);
    }
    this.#closed = true; this.#epoch += 1;
    this.clearTimer(); this.contentEl.empty();
    if (this.#focus?.isConnected) this.#focus.focus();
  }
  /** Plugin unload closes immediately without dispatching a replacement or cancelling the shared child. */
  public dispose(): void { this.#discardConfirmation = true; this.close(); }
  private unresolved(): boolean { return this.#mutation?.status === "pending" || this.#mutation?.status === "uncertain"; }
  private active(epoch: number): boolean { return !this.#closed && epoch === this.#epoch; }
  private clearTimer(): void { if (this.#timer !== null) clearTimeout(this.#timer); this.#timer = null; }
  private expired(): boolean { return this.#preview !== null && Date.parse(this.#preview.expires_at) <= Date.now(); }
  private historyRequired(): boolean { return discardedWrites.get(this.client)?.has(this.options.recordId) === true; }
  private canApply(): boolean {
    return !this.#closed && !this.#busy && this.#confirmed && this.#preview !== null
      && this.#preview.conflicts.length === 0 && !this.expired() && this.#mutation === null && !this.#receipt
      && !this.#freshRequired && !this.historyRequired() && this.client.supports("facet.vault.apply");
  }
  private async refreshPreview(): Promise<void> {
    if (this.#closed || this.#busy || this.unresolved() || this.#receipt) return;
    this.clearTimer(); const epoch = ++this.#epoch;
    this.#busy = true; this.#confirmed = false; this.#freshRequired = false; this.#preview = null; this.#mutation = null;
    this.#history = []; this.#historyLoaded = false; this.#historyCursor = null; this.#historyBusy = false; this.#historyMessage = "";
    this.#labels.clear(); this.#labelMessage = "";
    this.#message = "Loading reconciliation preview…"; this.render();
    if (!this.client.supports("facet.vault.preview")) {
      this.#busy = false; this.#message = "This runtime lacks Markdown facet reconciliation. Upgrade the runtime; existing Open Brain commands remain available.";
      this.render(); return;
    }
    try {
      const preview = await this.client.call("facet.vault.preview", { dto_version: 1, workspace_id: this.options.workspaceId, record_id: this.options.recordId });
      if (!this.active(epoch)) return;
      this.#preview = preview; this.#message = "Review every addition and removal before applying.";
      this.#busy = false; this.render(); this.scheduleExpiry(epoch);
      void this.loadLabels(epoch);
      if (this.historyRequired()) void this.loadHistory();
    } catch (error: unknown) {
      if (!this.active(epoch)) return;
      this.#busy = false; this.#message = failure(error); this.render();
    }
  }
  private scheduleExpiry(epoch: number): void {
    this.clearTimer();
    if (!this.#preview || this.expired()) return;
    this.#timer = setTimeout(() => {
      if (!this.active(epoch)) return;
      if (this.expired()) { this.#confirmed = false; this.render(); }
      else this.scheduleExpiry(epoch);
    }, Math.min(2_147_483_647, Math.max(1, Date.parse(this.#preview.expires_at) - Date.now())));
  }
  private async loadLabels(epoch: number): Promise<void> {
    if (!this.client.supports("facet.dimension.list") || !this.client.supports("facet.value.list")) return;
    // Labels are advisory; stable IDs and preview conflicts remain authoritative.
    try {
      const dimensions = await this.client.call("facet.dimension.list", { dto_version: 1, include_archived: true, limit: 50, cursor: null });
      if (!this.active(epoch)) return;
      for (const dimension of dimensions.items) {
        if (!this.active(epoch)) return;
        const values = await this.client.call("facet.value.list", { dto_version: 1, dimension_id: dimension.dimension_id, include_archived: true, limit: 50, cursor: null });
        if (!this.active(epoch)) return;
        for (const value of values.items) this.#labels.set(value.value_id, `${dimension.name}: ${value.name}${value.effective_archived ? " (archived)" : ""}`);
      }
      this.#labelMessage = "Vocabulary labels are advisory. Unloaded values are shown by stable ID.";
    } catch { if (!this.active(epoch)) return; this.#labelMessage = "Vocabulary labels are unavailable; stable IDs are shown."; }
    this.render();
  }
  private async apply(retry = false): Promise<void> {
    if (this.#closed || this.#busy) return;
    if (retry) { if (this.#mutation?.status !== "uncertain") return; }
    else {
      if (!this.canApply() || !this.#preview) { if (this.expired()) this.render(); return; }
      this.#mutation = this.client.createMutation("facet.vault.apply", { dto_version: 1, preview_id: this.#preview.preview_id });
    }
    const mutation = this.#mutation!; const epoch = this.#epoch;
    this.#busy = true; this.#discardConfirmation = false; this.#message = "Applying classification…"; this.render();
    try {
      const result = await (retry ? mutation.retry() : mutation.submit());
      if (!this.active(epoch)) return;
      this.#receipt = result; this.#confirmed = false;
      this.#message = result.projection_status === "refresh_required" ? "Classification committed; managed vault refresh needed." : "Classification committed; managed projection is current.";
      this.clearTimer();
    } catch (error: unknown) {
      if (!this.active(epoch)) return;
      this.#confirmed = false;
      this.#message = mutation.status === "uncertain" ? "The result is uncertain. Retry the same operation to recover its receipt; do not create a replacement write." : failure(error);
    } finally { if (this.active(epoch)) { this.#busy = false; this.render(); } }
  }
  private async refreshManaged(): Promise<void> {
    if (this.#closed || !this.#receipt || this.#refreshBusy || this.#receipt.projection_status !== "refresh_required") return;
    const epoch = this.#epoch; this.#refreshBusy = true; this.#message = "Classification committed; refreshing managed vault…"; this.render();
    try {
      await this.options.refreshManaged();
      if (!this.active(epoch)) return;
      this.#receipt = { ...this.#receipt, projection_status: "current" };
      this.#message = "Classification committed; managed vault refreshed.";
    } catch { if (this.active(epoch)) this.#message = "Classification committed; vault refresh failed. Retry refresh only. Edited notes and generated-index conflicts must be preserved."; }
    finally { if (this.active(epoch)) { this.#refreshBusy = false; this.render(); } }
  }
  private async editNote(): Promise<void> {
    if (this.#closed || this.#busy || this.unresolved() || this.#receipt || !this.options.editNote) return;
    const epoch = this.#epoch; this.#busy = true; this.#confirmed = false; this.#freshRequired = true; this.render();
    try { await this.options.editNote(); if (this.active(epoch)) this.#message = "Edit the note, then request a fresh preview before applying."; }
    catch { if (this.active(epoch)) this.#message = "The note could not be opened. Your preview and note edits are preserved."; }
    finally { if (this.active(epoch)) { this.#busy = false; this.render(); } }
  }
  private async loadHistory(): Promise<void> {
    if (this.#closed || !this.#preview || this.#historyBusy || this.#history.length >= 500 || (this.#historyLoaded && this.#historyCursor === null)) return;
    if (!this.client.supports("facet.history.list")) { this.#historyMessage = "Classification history is unavailable. Upgrade the runtime before replacing an uncertain write."; this.render(); return; }
    const epoch = this.#epoch; const cursor = this.#historyCursor; this.#historyBusy = true; this.#historyMessage = "Loading classification history…"; this.render();
    try {
      const page = await this.client.call("facet.history.list", { dto_version: 1, subject: this.#preview.subject, cursor, limit: 50 });
      if (!this.active(epoch)) return;
      this.#history.push(...page.items); this.#historyCursor = page.next_cursor; this.#historyLoaded = true;
      discardedWrites.get(this.client)?.delete(this.options.recordId);
      this.#historyMessage = this.#history.length === 0 ? "No classification events." : `Loaded ${this.#history.length} classification events. History is read-only.`;
    } catch (error: unknown) { if (this.active(epoch)) this.#historyMessage = errorCode(error) === "cursor_stale" ? "History changed. Refresh the preview to restart history." : "Classification history could not be loaded. Retry history."; }
    finally { if (this.active(epoch)) { this.#historyBusy = false; this.render(); } }
  }
  private render(): void {
    if (this.#closed) return;
    const renderVersion = ++this.#renderVersion;
    const focused = this.contentEl.ownerDocument.activeElement;
    const focusAction = focused instanceof Object && "getAttribute" in focused ? (focused as HTMLElement).getAttribute("data-facet-action") : null;
    this.contentEl.empty(); this.contentEl.addClass("open-brain-facet-reconciliation");
    const status = this.contentEl.createEl("p", { text: this.#message }); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    this.contentEl.createEl("p", { text: "Only explicit registered facet metadata is reviewed. Body edits require the content workflow. Unknown labels do not create vocabulary." });
    const button = (label: string, action: string, disabled: boolean, run: () => void): HTMLButtonElement => {
      const control = this.contentEl.createEl("button", { text: label }); control.setAttribute("type", "button"); control.setAttribute("data-facet-action", action); control.disabled = disabled;
      control.addEventListener("click", () => { if (!this.#closed && renderVersion === this.#renderVersion && !control.disabled) run(); });
      if (focusAction === action) control.focus(); return control;
    };
    if (this.#preview) {
      this.contentEl.createEl("p", { text: `Current classification version: ${this.#preview.expected_version}` });
      this.contentEl.createEl("p", { text: `Preview expires: ${this.#preview.expires_at}` });
      for (const [heading, ids] of [["Proposed additions", this.#preview.add_value_ids], ["Proposed removals", this.#preview.remove_value_ids]] as const) {
        this.contentEl.createEl("h3", { text: heading }); const list = this.contentEl.createEl("ul");
        for (const id of ids) list.createEl("li", { text: `${this.#labels.get(id) ?? "Value"} · ${id}` });
        if (ids.length === 0) list.createEl("li", { text: "None" });
      }
      this.contentEl.createEl("p", { text: "Missing or empty facets means removal of all current memberships. Every proposed removal is listed above." });
      if (this.#labelMessage) this.contentEl.createEl("p", { text: this.#labelMessage });
      for (const conflict of this.#preview.conflicts) this.contentEl.createEl("p", { text: `Conflict: ${conflicts[conflict.code]}${conflict.value_id ? ` (${this.#labels.get(conflict.value_id) ?? conflict.value_id})` : ""}` });
      if (this.expired() && !this.#receipt) this.contentEl.createEl("p", { text: "Preview expired. Request a fresh preview and confirm it again." });
      if (this.historyRequired()) this.contentEl.createEl("p", { text: "An earlier write was discarded while uncertain. Reload classification history before confirming a replacement." });
      if (!this.#receipt && this.#mutation === null) {
        const label = this.contentEl.createEl("label"); const check = label.createEl("input", { type: "checkbox" });
        check.setAttribute("data-facet-action", "confirm"); check.checked = this.#confirmed;
        check.disabled = this.#busy || this.#freshRequired || this.#preview.conflicts.length > 0 || this.expired() || this.historyRequired();
        label.createSpan({ text: " I reviewed these additions and removals for this preview." });
        check.addEventListener("change", () => { if (!this.#closed && renderVersion === this.#renderVersion && !check.disabled) { this.#confirmed = check.checked; this.render(); } });
        if (focusAction === "confirm") check.focus();
      }
    }
    if (!this.#receipt) {
      if (!this.client.supports("facet.vault.apply")) this.contentEl.createEl("p", { text: "Apply is unavailable in this runtime. Upgrade the runtime to apply reviewed facet edits." });
      button("Apply reviewed facets", "apply", !this.canApply(), () => { void this.apply(); });
      if (this.#mutation?.status === "uncertain") button("Retry same operation", "retry", this.#busy, () => { void this.apply(true); });
      button("Refresh preview", "preview", this.#busy || this.unresolved(), () => { void this.refreshPreview(); });
      if (this.options.editNote) button("Edit note", "edit", this.#busy || this.unresolved(), () => { void this.editNote(); });
    } else {
      this.contentEl.createEl("p", { text: `Receipt: ${this.#receipt.receipt.status}; operation ${this.#receipt.receipt.operation_id}; classification version ${this.#receipt.receipt.version}; event ${this.#receipt.receipt.event_id ?? "none"}.` });
      if (this.#receipt.projection_status === "refresh_required") button("Refresh managed vault", "refresh", this.#refreshBusy, () => { void this.refreshManaged(); });
    }
    if (this.#preview) {
      this.contentEl.createEl("h3", { text: "Classification history" });
      this.contentEl.createEl("p", { text: this.#historyMessage || (this.client.supports("facet.history.list") ? "History is read-only. It records classification events, not content revisions." : "Classification history is unavailable in this runtime.") });
      const history = this.contentEl.createDiv({ cls: "open-brain-facet-history" }); history.style.maxHeight = "18rem"; history.style.overflow = "auto";
      for (const event of this.#history) { history.createEl("p", { text: `Sequence ${event.sequence} · ${event.kind} · ${event.recorded_at} · ${event.event_id}` }); history.createEl("pre", { text: JSON.stringify(event.changes, null, 2) }); }
      if (!this.#historyLoaded || this.#historyCursor !== null) button(this.#historyLoaded ? "Load more history" : "Load classification history", "history", this.#historyBusy || this.#history.length >= 500 || !this.client.supports("facet.history.list"), () => { void this.loadHistory(); });
      if (this.#history.length >= 500 && this.#historyCursor) this.contentEl.createEl("p", { text: "500-event display limit reached. Refresh to restart history." });
    }
    button(this.#discardConfirmation && this.unresolved() ? "Close and discard retry state" : "Close", "close", false, () => this.close());
  }
}

export function openFacetReconciliation(app: App, client: FacetClient, options: FacetReconciliationOptions): FacetReconciliationModal {
  const modal = new FacetReconciliationModal(app, client, options); modal.open(); return modal;
}
