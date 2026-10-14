import { randomUUID } from "node:crypto";
import { Modal, type App } from "obsidian";
import { FacetClient, type FacetMutation } from "./facet-client";
import { parseFacetPublicationInspection, type FacetPublicationInspection } from "./facet-inspection";
import type { Wire_assignment_state, Wire_saved_query, Wire_review_propose_from_facet_query_response } from "./facet-types";
import { FacetWireError, identifier } from "./facet-wire";
import { listSpaces, type PublicationResult, type SpaceItem } from "./t07-client";

export type FacetPublicationTarget =
  | { kind: "create"; queryId: string }
  | { kind: "update"; pageId: string; recordId: string; spaceId: string };
export interface FacetPublicationHooks {
  capabilities: ReadonlySet<string>;
  /** Refresh explicitly, find the committed page, wait for managed-file availability, then open it. */
  refreshAndOpen(pageId: string, cancelled: () => boolean): Promise<void>;
  onClose?(): void;
}
export type FacetPublicationState = "loading" | "editing" | "proposing" | "proposal_uncertain"
  | "inspection_error" | "review" | "deciding" | "decision_uncertain" | "stale"
  | "published" | "refreshing" | "refresh_required" | "rejected" | "error" | "unavailable" | "disposed";
type Decision = "approve" | "reject" | "edit_and_approve";
const codeOf = (error: unknown): string => typeof error === "object" && error !== null && "code" in error ? String(error.code) : "transport_failed";
const staleCodes = new Set(["review_conflict", "version_conflict", "revision_changed", "query_archived", "facet_archived"]);
const refusalCodes = new Set(["invalid_arguments", "not_found", "unsupported_capability", "idempotency_conflict", "limit_exceeded", ...staleCodes]);
const uncertaintyWarning = "Closing this dialog or unloading the plugin discards the draft and in-memory retry request. An uncertain write may already have committed. Reload authoritative state before replacing it.";

/** Session-only publication state. A read failure cannot create a replacement write. */
export class FacetPublicationSession {
  public state: FacetPublicationState = "loading";
  public message = "Loading saved query…";
  public title = "";
  public markdown = "";
  public destinationSpaceId = "";
  public query: Wire_saved_query | null = null;
  public pageAssignments: Wire_assignment_state | null = null;
  public spaces: SpaceItem[] = [];
  public inspection: FacetPublicationInspection | null = null;
  public proposal: Wire_review_propose_from_facet_query_response | null = null;
  public publication: PublicationResult | null = null;
  public proposalMutation: FacetMutation<"review.propose_from_facet_query"> | null = null;
  public decisionRequest: Readonly<{ operation: `publication.${Decision}`; args: Readonly<Record<string, unknown>> }> | null = null;
  #closed = false;
  #busy = false;
  public constructor(public readonly client: FacetClient, public readonly target: FacetPublicationTarget,
    public readonly hooks: FacetPublicationHooks, private readonly changed: () => void = () => {}) {}
  public get uncertain(): boolean { return ["proposing", "deciding", "proposal_uncertain", "decision_uncertain"].includes(this.state); }
  public get busy(): boolean { return this.#busy; }
  private set(state: FacetPublicationState, message: string): void {
    if (this.#closed) return;
    this.state = state; this.message = message; this.changed();
  }
  private async loadInputs(): Promise<void> {
    let queryId: string;
    if (this.target.kind === "update") {
      const target = this.target;
      const [attachment, assignments] = await Promise.all([
        this.client.call("facet.query.attachment.get", { dto_version: 1, page_id: target.pageId }),
        this.client.call("facet.assignment.get", { dto_version: 1, record_id: target.recordId }),
      ]);
      if (this.#closed) return;
      if (attachment.item === null || attachment.item.page_id !== target.pageId
        || assignments.item.subject.kind !== "canonical" || assignments.item.subject.id !== target.pageId
        || assignments.item.record_id !== target.recordId) throw new FacetWireError("not_found");
      this.pageAssignments = assignments.item;
      this.destinationSpaceId = target.spaceId;
      queryId = attachment.item.query_id;
    } else queryId = this.target.queryId;
    const query = await this.client.call("facet.query.get", { dto_version: 1, query_id: queryId });
    if (this.#closed) return;
    if (query.item.archived) throw new FacetWireError("query_archived");
    this.query = query.item;
  }
  public async load(): Promise<void> {
    if (this.#closed || this.#busy || this.state !== "loading") return;
    const needed = ["facet.query.get", "review.propose_from_facet_query"] as const;
    if (needed.some((operation) => !this.client.supports(operation)) || !this.hooks.capabilities.has("publication.show")
      || (this.target.kind === "create" && !this.hooks.capabilities.has("space.list"))
      || (this.target.kind === "update" && (!this.client.supports("facet.query.attachment.get") || !this.client.supports("facet.assignment.get")))) {
      this.set("unavailable", "This runtime lacks the facet publication operations needed here. Upgrade the local runtime."); return;
    }
    this.#busy = true;
    try {
      await this.loadInputs();
      if (this.#closed) return;
      if (this.target.kind === "create") this.spaces = await listSpaces({ invoke: async <T>(operation: Parameters<FacetClient["invoke"]>[0], args: Record<string, unknown>, timeoutMs?: number): Promise<T> => {
        if (this.#closed) throw new FacetWireError("cancelled");
        const result = await this.client.invoke<T>(operation, args, timeoutMs);
        if (this.#closed) throw new FacetWireError("cancelled");
        return result;
      } });
      this.set("editing", "Write a title and Markdown draft. Proposal creation freezes the complete eligible source set in the engine.");
    } catch (error) { this.set("error", `Cannot load publication: ${codeOf(error)}. Your draft is retained.`); }
    finally { this.#busy = false; if (!this.#closed) this.changed(); }
  }
  public async propose(fresh = false): Promise<void> {
    if (this.#closed || this.#busy || !(this.state === "editing" || (fresh && this.state === "stale"))) return;
    if (!this.title.trim() || !this.markdown.trim() || (this.target.kind === "create" && !this.spaces.some((s) => s.space_id === this.destinationSpaceId))) {
      this.set(this.state, "Enter a title, Markdown draft and an explicit destination space."); return;
    }
    this.#busy = true;
    this.set("proposing", fresh ? "Loading current inputs for a fresh proposal…" : "Freezing the complete selection…");
    try {
      if (fresh) await this.loadInputs();
      if (this.#closed || this.query === null) return;
      this.inspection = null; this.decisionRequest = null; this.proposal = null;
      this.proposalMutation = this.client.createMutation("review.propose_from_facet_query", {
        dto_version: 1, query_id: this.query.query_id, expected_query_version: this.query.version,
        target_page_id: this.target.kind === "update" ? this.target.pageId : null,
        expected_page_revision_id: this.target.kind === "update" ? this.pageAssignments!.revision_id : null,
        destination_space_id: this.target.kind === "create" ? this.destinationSpaceId : null,
        title: this.title, markdown: this.markdown,
      });
      await this.submitProposal(false);
    } catch (error) { this.proposalError(error); }
    finally { this.#busy = false; if (!this.#closed) this.changed(); }
  }
  private proposalError(error: unknown): void {
    const code = codeOf(error);
    if (this.proposalMutation?.status === "uncertain") this.set("proposal_uncertain", `Proposal result uncertain (${code}). Retry the same operation. ${uncertaintyWarning}`);
    else if (staleCodes.has(code)) this.set("stale", "The proposal inputs are stale. Your draft is retained. Create and inspect an explicit fresh proposal.");
    else this.set("editing", code === "limit_exceeded" || code === "invalid_arguments"
      ? "Publication requires 1–32 eligible sources. Change the saved query or check the draft; your draft is retained."
      : `Proposal refused (${code}). Your draft is retained.`);
  }
  private async submitProposal(retry: boolean): Promise<void> {
    const mutation = this.proposalMutation!;
    this.proposal = await (retry ? mutation.retry() : mutation.submit());
    if (this.#closed) return;
    await this.inspect();
  }
  public async retryProposal(): Promise<void> {
    if (this.#closed || this.#busy || this.state !== "proposal_uncertain") return;
    this.#busy = true; this.set("proposing", "Retrying the same proposal operation…");
    try { await this.submitProposal(true); } catch (error) { this.proposalError(error); }
    finally { this.#busy = false; if (!this.#closed) this.changed(); }
  }
  private async inspect(): Promise<void> {
    try {
      const inspection = parseFacetPublicationInspection(await this.client.invoke("publication.show", { dto_version: 1, proposal_id: this.proposal!.proposal_id }, 30_000));
      if (this.#closed) return;
      const proposed = this.proposal!;
      if (inspection.proposal_id !== proposed.proposal_id || inspection.page_id !== proposed.page_id
        || inspection.operation !== this.target.kind || inspection.space_id !== this.destinationSpaceId
        || inspection.facet_selection.query_id !== this.query!.query_id
        || inspection.facet_selection.query_version !== this.query!.version
        || (this.target.kind === "update" && inspection.target_page_id !== this.target.pageId)) throw new FacetWireError("protocol_error");
      if (inspection.proposal_status !== "pending") throw new FacetWireError("review_conflict");
      this.inspection = inspection;
      this.set("review", "Inspect the actual frozen selected sources and inherited provenance before deciding. Draft edits keep this evidence frozen.");
    } catch (error) {
      this.inspection = null;
      this.set(staleCodes.has(codeOf(error)) ? "stale" : "inspection_error", staleCodes.has(codeOf(error))
        ? "This proposal is stale. Retain the draft and explicitly create a fresh proposal. Reinspection cannot repair frozen evidence."
        : `Proposal created; inspection unavailable (${codeOf(error)}). Retry inspection before any decision.`);
    }
  }
  public async retryInspection(): Promise<void> {
    if (this.#closed || this.#busy || this.state !== "inspection_error") return;
    this.#busy = true;
    try { await this.inspect(); } finally { this.#busy = false; if (!this.#closed) this.changed(); }
  }
  public async decide(decision: Decision): Promise<void> {
    if (this.#closed || this.#busy || this.state !== "review" || this.inspection === null) return;
    const operation = `publication.${decision}` as const;
    if (!this.hooks.capabilities.has(operation)) { this.set("review", "This decision is unavailable in the current session."); return; }
    // Approval cannot silently discard owner edits made after inspection.
    if (decision === "approve" && this.markdown !== this.inspection.markdown) { this.set("review", "Use Edit and approve to publish the edited Markdown with this frozen evidence."); return; }
    const args: Record<string, unknown> = { dto_version: 1, idempotency_key: `obsidian-facet-${decision}-${randomUUID()}`,
      proposal_id: this.inspection.proposal_id, review_token: this.inspection.review_token };
    if (decision === "edit_and_approve") args.markdown = this.markdown;
    this.decisionRequest = Object.freeze({ operation, args: Object.freeze(args) });
    await this.submitDecision();
  }
  private async submitDecision(): Promise<void> {
    this.#busy = true; this.set("deciding", "Submitting the review decision…");
    try {
      const request = this.decisionRequest!;
      const result = await this.client.invoke<Record<string, unknown>>(request.operation, { ...request.args }, 30_000);
      if (this.#closed) return;
      if (result === null || typeof result !== "object" || result.proposal_id !== request.args.proposal_id) throw new FacetWireError("protocol_error");
      if (request.operation === "publication.reject") {
        if (result.status !== "rejected") throw new FacetWireError("protocol_error");
        this.set("rejected", "Proposal rejected. No page was published."); return;
      }
      if (result.status !== (request.operation === "publication.approve" ? "approved" : "edited")
        || result.page_id !== this.inspection!.page_id || !identifier(result.publication_id, ["publication"])) throw new FacetWireError("protocol_error");
      this.publication = { page_id: result.page_id as string, proposal_id: result.proposal_id as string, publication_id: result.publication_id };
      this.set("published", "Published. Refreshing and opening the managed note…");
      await this.refresh();
    } catch (error) {
      const code = codeOf(error);
      if (staleCodes.has(code)) { this.inspection = null; this.set("stale", "The frozen evidence is stale. Your draft is retained. Explicitly create and inspect a fresh proposal."); }
      else this.set(refusalCodes.has(code) ? "review" : "decision_uncertain", refusalCodes.has(code)
        ? `Decision refused (${code}). Your draft is retained.`
        : `Decision result uncertain (${code}). Retry the same decision. ${uncertaintyWarning}`);
    } finally { this.#busy = false; if (!this.#closed) this.changed(); }
  }
  public async retryDecision(): Promise<void> {
    if (!this.#closed && !this.#busy && this.state === "decision_uncertain") await this.submitDecision();
  }
  private async refresh(): Promise<void> {
    if (this.#closed || this.publication === null) return;
    this.set("refreshing", "Published. Refreshing and opening the managed note…");
    try { await this.hooks.refreshAndOpen(this.publication.page_id, () => this.#closed); this.set("published", "Published and opened the managed note."); }
    catch { this.set("refresh_required", "Published; vault refresh needed. Retry refresh and open only."); }
  }
  public async retryRefresh(): Promise<void> {
    if (this.#closed || this.#busy || this.state !== "refresh_required") return;
    this.#busy = true;
    try { await this.refresh(); } finally { this.#busy = false; if (!this.#closed) this.changed(); }
  }
  public dispose(): void { this.#closed = true; this.state = "disposed"; }
}

export class FacetPublicationModal extends Modal {
  public readonly session: FacetPublicationSession;
  #returnFocus: HTMLElement | null = null;
  #disposed = false;
  #discardConfirmed = false;
  public constructor(app: App, client: FacetClient, target: FacetPublicationTarget, hooks: FacetPublicationHooks) {
    super(app);
    this.session = new FacetPublicationSession(client, target, hooks, () => this.render());
  }
  public override onOpen(): void {
    this.#returnFocus = typeof document !== "undefined" && document.activeElement instanceof HTMLElement ? document.activeElement : null;
    this.titleEl.setText(this.session.target.kind === "create" ? "Publish saved facet query" : "Update page from attached facet query");
    this.render(); void this.session.load();
  }
  public override close(): void {
    if (this.session.uncertain && !this.#discardConfirmed) {
      this.contentEl.createEl("p", { text: uncertaintyWarning });
      this.button("Discard draft and close", () => { this.#discardConfirmed = true; this.close(); });
      return;
    }
    super.close();
  }
  public override onClose(): void {
    if (this.#disposed) return;
    this.#disposed = true; this.session.dispose(); this.contentEl.empty();
    this.#returnFocus?.focus(); this.session.hooks.onClose?.();
  }
  /** Plugin unload stops continuations without killing the shared bridge. */
  public dispose(): void { this.#discardConfirmed = true; this.close(); }
  private button(text: string, action: () => void, disabled = false): void {
    const button = this.contentEl.createEl("button", { text });
    button.type = "button"; button.disabled = disabled;
    button.addEventListener("click", () => { if (!button.disabled && !this.#disposed) action(); });
  }
  private field(label: string, value: string, tag: "input" | "textarea", change: (value: string) => void, disabled: boolean): void {
    const container = this.contentEl.createEl("label");
    container.createSpan({ text: label });
    const input = container.createEl(tag);
    input.setAttribute("aria-label", label); input.value = value; input.disabled = disabled;
    if (tag === "textarea") (input as HTMLTextAreaElement).rows = 12;
    input.addEventListener("input", () => change(input.value));
  }
  private render(): void {
    if (this.#disposed) return;
    const previousLabel = typeof document !== "undefined" && this.contentEl.contains(document.activeElement) ? document.activeElement?.getAttribute("aria-label") : null;
    this.contentEl.empty(); this.contentEl.addClass("open-brain-facet-publication");
    const s = this.session;
    const status = this.contentEl.createEl("p", { text: s.message });
    status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    this.contentEl.createEl("p", { text: "Drafts and retry requests remain only in this loaded session. Closing or unloading discards them; cancellation after dispatch is not rollback." });
    if (s.query) {
      this.contentEl.createEl("h3", { text: "Source-selection query" });
      this.contentEl.createEl("p", { text: `${s.query.name} (${s.query.query_id}), version ${s.query.version}. Match all selected values.` });
      this.contentEl.createEl("pre", { text: JSON.stringify(s.query.selector, null, 2) });
    }
    if (s.target.kind === "update") {
      this.contentEl.createEl("p", { text: `Target: ${s.target.pageId}. Fixed destination: ${s.target.spaceId}. Current revision: ${s.pageAssignments?.revision_id ?? "loading"}.` });
      this.contentEl.createEl("h3", { text: "Page's own facet assignments" });
      this.contentEl.createEl("p", { text: s.pageAssignments?.memberships.map((m) => `${m.value_id} (membership version ${m.membership_version})`).join("\n") || "No facet assignments on this page." });
    } else if (s.spaces.length > 0) {
      const label = this.contentEl.createEl("label"); label.createSpan({ text: "Destination space" });
      const select = label.createEl("select"); select.setAttribute("aria-label", "Destination space");
      select.createEl("option", { text: "Choose destination explicitly", value: "" });
      for (const space of s.spaces) select.createEl("option", { text: space.name, value: space.space_id });
      select.value = s.destinationSpaceId; select.disabled = s.busy || s.state !== "editing";
      select.addEventListener("change", () => { s.destinationSpaceId = select.value; });
    } else if (s.state === "editing") this.contentEl.createEl("p", { text: "No destination spaces available. Create a space before starting publication." });
    const editable = !s.busy && ["editing", "review", "stale"].includes(s.state);
    this.field("Owner-written title", s.title, "input", (value) => { s.title = value; }, !editable || s.state === "review");
    this.field("Owner-written Markdown draft", s.markdown, "textarea", (value) => { s.markdown = value; }, !editable);
    if (s.inspection) {
      const inspection = s.inspection;
      this.contentEl.createEl("h3", { text: "Actual frozen proposal" });
      this.contentEl.createEl("p", { text: `${inspection.operation}: ${inspection.page_id}, destination ${inspection.space_id}. Proposal ${inspection.proposal_id}.` });
      this.contentEl.createEl("p", { text: `Frozen title: ${inspection.title}` });
      this.contentEl.createEl("h3", { text: "Frozen proposal Markdown" });
      this.contentEl.createEl("pre", { text: inspection.markdown });
      const evidence = this.contentEl.createDiv({ cls: "open-brain-facet-evidence" });
      evidence.setAttribute("tabindex", "0"); evidence.setAttribute("aria-label", "Frozen publication evidence");
      for (const selected of [true, false]) {
        evidence.createEl("h3", { text: selected ? "Newly selected evidence" : "Inherited provenance" });
        const rows = inspection.evidence.filter((e) => inspection.selected_capture_ids.includes(e.capture_id) === selected);
        if (rows.length === 0) evidence.createEl("p", { text: "None." });
        for (const row of rows) {
          const state = inspection.facet_selection.source_states.find((source) => source.capture_id === row.capture_id)!;
          evidence.createEl("p", { text: `${row.capture_id} · source revision ${row.capture_id} · SHA-256 ${state.sha256} · source space ${state.space_id ?? "unrouted"}` });
          evidence.createEl("pre", { text: row.excerpt });
        }
      }
      evidence.createEl("h3", { text: "Frozen query selection and facet evidence" });
      evidence.createEl("pre", { text: JSON.stringify({ query_id: inspection.facet_selection.query_id,
        query_version: inspection.facet_selection.query_version, selection_version: inspection.facet_selection.query_selection_version,
        attachment_version: inspection.facet_selection.attachment_version, selector: inspection.facet_selection.selector,
        source_states: inspection.facet_selection.facet_source_states }, null, 2) });
    }
    if (s.state === "editing") this.button("Create proposal and inspect", () => { void s.propose(); }, s.busy);
    if (s.state === "stale") this.button("Create fresh proposal and inspect", () => { void s.propose(true); }, s.busy);
    if (s.state === "inspection_error") this.button("Retry inspection", () => { void s.retryInspection(); }, s.busy);
    if (s.state === "proposal_uncertain") this.button("Retry same proposal operation", () => { void s.retryProposal(); }, s.busy);
    if (s.state === "decision_uncertain") this.button("Retry same decision", () => { void s.retryDecision(); }, s.busy);
    if (s.state === "refresh_required") this.button("Retry refresh and open", () => { void s.retryRefresh(); }, s.busy);
    if (s.state === "review") {
      for (const [label, decision] of [["Approve", "approve"], ["Edit and approve", "edit_and_approve"], ["Reject", "reject"]] as const)
        this.button(label, () => { void s.decide(decision); }, s.busy || !s.hooks.capabilities.has(`publication.${decision}`));
    }
    this.button("Close", () => this.close());
    if (previousLabel) Array.from(this.contentEl.querySelectorAll<HTMLElement>("[aria-label]")).find((el) => el.getAttribute("aria-label") === previousLabel)?.focus();
  }
}

export function openFacetPublication(app: App, client: FacetClient, target: FacetPublicationTarget, hooks: FacetPublicationHooks): FacetPublicationModal {
  const modal = new FacetPublicationModal(app, client, target, hooks); modal.open(); return modal;
}
