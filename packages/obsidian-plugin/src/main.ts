import { realpath } from "node:fs/promises";

import {
  App,
  type EventRef,
  FileSystemAdapter,
  FuzzySuggestModal,
  Modal,
  Notice,
  Plugin,
  PluginSettingTab,
  Setting,
  TFile,
  normalizePath,
} from "obsidian";

import {
  BridgeError,
  OpenBrainBridge,
  discoverOpenBrainExecutable,
} from "./bridge";
import { openCanvasFile } from "./canvas";
import { captureText } from "./capture";
import {
  type ConflictReview,
  type ConflictSummary,
  type Exclusion,
  type Handshake,
  type SearchItem,
  type ProviderSelection,
  type ProviderStatus,
  type SuggestionReview,
  type SuggestionSummary,
  parseCanvas,
  parseConflictReview,
  parseConflicts,
  parseExclusions,
  parseHandshake,
  parseProviderStatus,
  parseReconcile,
  parseReview,
  parseSearch,
  parseSemanticRefresh,
  parseSuggestions,
  parseWorkspaceStatus,
  record,
  safeRelativePath,
} from "./contracts";
import { RefreshScheduler, type RefreshReason } from "./refresh-scheduler";
import {
  clientCapabilities,
  createSpace,
  decidePublication,
  deterministicDraft,
  inspectPublication,
  listInbox,
  listSpaces,
  proposePublication,
  preparePublicationWorkspace,
  readCompleteRecord,
  refreshAndFindApprovedNote,
  requireCapabilities,
  routeCaptures,
  searchPage,
  type InboxItem,
  type PublicationInspection,
  type RecordSummary,
  type SearchRequest,
  type SpaceItem,
} from "./t07-client";

const CANVAS_PATH = normalizePath("Open Brain Graph.canvas");
const DISPLAY_CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/gu;
const FILE_REGISTRATION_TIMEOUT_MS = 10_000;
const OWNED_CANVAS_HEADING = "# Open Brain graph";
const PROVIDER_LABELS: Record<string, string> = {
  anthropic_api: "Anthropic API key",
  claude_subscription: "Claude subscription",
  gemini_api: "Google Gemini API key",
  openai_api: "OpenAI API key",
};

interface OpenBrainSettings {
  executablePath: string;
  inferencePaused: boolean;
}

export interface ReviewCommandOverrides {
  bridge?: OpenBrainBridge;
  capabilities?: Set<string>;
  sameVault?: (path: string) => Promise<boolean>;
}

const DEFAULT_SETTINGS: OpenBrainSettings = {
  executablePath: "",
  inferencePaused: false,
};

export default class OpenBrainPlugin extends Plugin {
  public override settings: OpenBrainSettings = DEFAULT_SETTINGS;
  #bridge: OpenBrainBridge | null = null;
  #scheduler: RefreshScheduler | null = null;
  #providerSelection: ProviderSelection | null = null;
  #handshake: Handshake | null = null;
  #pendingFileWaits = new Set<() => void>();

  public override async onload(): Promise<void> {
    this.settings = parseSettings(await this.loadData());
    this.addSettingTab(new OpenBrainSettingTab(this.app, this));
    this.#scheduler = new RefreshScheduler((reason) => this.refreshPresentation(reason));
    this.#registerCommands();
    this.app.workspace.onLayoutReady(() => {
      this.#registerVaultEvents();
      void this.#connectOnStartup();
    });
  }

  public override onunload(): void {
    this.#scheduler?.dispose();
    this.#scheduler = null;
    for (const cancel of [...this.#pendingFileWaits]) cancel();
    this.#bridge?.dispose();
    this.#bridge = null;
    this.#providerSelection = null;
    this.#handshake = null;
  }

  async updateSettings(update: Partial<OpenBrainSettings>): Promise<void> {
    this.settings = { ...this.settings, ...update };
    await this.saveData(this.settings);
  }

  resetBridge(): void {
    this.#bridge?.dispose();
    this.#bridge = null;
    this.#providerSelection = null;
    this.#handshake = null;
  }

  async refreshPresentation(reason: RefreshReason): Promise<void> {
    try {
      const bridge = await this.#managedBridge();
      const reconciliation = parseReconcile(
        await bridge.invoke("workspace.reconcile", {}, 30_000),
      );
      await bridge.invoke("graph.refresh_structural", {}, 90_000);
      if (
        this.#providerSelection !== null &&
        (reason === "manual" || !this.settings.inferencePaused)
      ) {
        try {
          const semantic = parseSemanticRefresh(
            await bridge.invoke("graph.refresh_semantic", {}, 90_000),
          );
          if (semantic.status === "failed") {
            new Notice(
              `Open Brain semantic refresh failed (${semantic.reason ?? "provider_failure"}). Structural results remain available.`,
              10_000,
            );
          }
        } catch (error) {
          if (!(error instanceof BridgeError) || error.code !== "setup_required") {
            throw error;
          }
          this.#providerSelection = null;
          new Notice(
            "Open Brain provider access must be configured again after the bridge restarted. Structural results remain available.",
            10_000,
          );
        }
      }
      const canvas = parseCanvas(await bridge.invoke("graph.canvas", {}, 15_000));
      await this.#writeCanvas(canvas.canvas);
      if (reason === "manual") {
        new Notice(
          `Open Brain graph refreshed. ${reconciliation.accepted_note_ids.length} edited note(s) accepted.`,
        );
      }
      if (reconciliation.missing_note_ids.length > 0) {
        new Notice(
          `${reconciliation.missing_note_ids.length} managed note(s) are missing. Deactivation requires an explicit owner action.`,
        );
      }
    } catch (error) {
      this.#noticeError(error);
    }
  }

  async openManagedFile(relativePath: string): Promise<void> {
    if (!safeRelativePath(relativePath)) throw new BridgeError("invalid_source_path");
    await this.#managedBridge();
    const file = await this.#waitForVaultFile(normalizePath(relativePath));
    await openCanvasFile(this.app.workspace, file);
  }

  async #waitForVaultFile(path: string): Promise<TFile> {
    const current = this.app.vault.getAbstractFileByPath(path);
    if (current instanceof TFile) return current;
    return await new Promise<TFile>((resolve, reject) => {
      let settled = false;
      let timer: ReturnType<typeof setTimeout> | null = null;
      let eventRef: EventRef | null = null;
      const finish = (file: TFile | null, error?: BridgeError): void => {
        if (settled) return;
        settled = true;
        if (timer !== null) clearTimeout(timer);
        if (eventRef !== null) this.app.vault.offref(eventRef);
        this.#pendingFileWaits.delete(cancel);
        if (file !== null) resolve(file);
        else reject(error ?? new BridgeError("source_unavailable"));
      };
      const cancel = (): void => finish(null, new BridgeError("bridge_closed"));
      eventRef = this.app.vault.on("create", (file) => {
        if (file instanceof TFile && file.path === path) finish(file);
      });
      this.#pendingFileWaits.add(cancel);
      try {
        const registered = this.app.vault.getAbstractFileByPath(path);
        if (registered instanceof TFile) {
          finish(registered);
          return;
        }
        timer = setTimeout(() => finish(null), FILE_REGISTRATION_TIMEOUT_MS);
      } catch {
        finish(null);
      }
    });
  }

  async reviewSuggestion(suggestionId: string): Promise<void> {
    try {
      const bridge = await this.#managedBridge();
      const review = parseReview(
        await bridge.invoke("graph.review", { suggestion_id: suggestionId }),
      );
      new SuggestionReviewModal(this.app, this, review).open();
    } catch (error) {
      this.#noticeError(error);
    }
  }

  async acceptSuggestion(review: SuggestionReview): Promise<void> {
    try {
      if (review.revision_status !== "current") {
        throw new BridgeError("stale_suggestion");
      }
      const bridge = await this.#managedBridge();
      await bridge.invoke(
        "graph.accept",
        { suggestion_id: review.suggestion_id },
        30_000,
      );
      await this.refreshPresentation("manual");
    } catch (error) {
      this.#noticeError(error);
      throw error;
    }
  }

  async resolveConflict(
    review: ConflictReview,
    choice: "accepted" | "workspace",
  ): Promise<void> {
    try {
      const bridge = await this.#managedBridge();
      const result = record(
        await bridge.invoke(
          "workspace.resolve",
          {
            choice,
            conflict_id: review.conflict_id,
            note_id: review.note_id,
          },
          30_000,
        ),
      );
      if (result.status !== "resolved" || result.choice !== choice) {
        throw new BridgeError("protocol_error");
      }
      new Notice(
        choice === "accepted"
          ? "Restored the accepted Open Brain version."
          : "Accepted the current vault edit.",
      );
      await this.refreshPresentation("manual");
    } catch (error) {
      this.#noticeError(error);
      throw error;
    }
  }

  #registerCommands(): void {
    this.addCommand({
      callback: () => void this.#initialize(),
      id: "initialize-managed-vault",
      name: "Initialize or locate managed vault",
    });
    this.addCommand({
      callback: () => void this.#capture(),
      id: "capture",
      name: "Capture text",
    });
    this.addCommand({
      callback: () => void this.reviewCaptures(),
      id: "review-captures-for-publication",
      name: "Review captures for publication",
    });
    this.addCommand({
      callback: () => void this.#searchRecords(),
      id: "search-records",
      name: "Search records with filters and paging",
    });
    this.addCommand({
      callback: () => void this.#search(),
      id: "search",
      name: "Search",
    });
    this.addCommand({
      callback: () => void this.#scheduler?.manual(),
      id: "refresh-graph",
      name: "Refresh graph now",
    });
    this.addCommand({
      callback: () => void this.#showSuggestions(),
      id: "review-suggestions",
      name: "Review graph suggestions",
    });
    this.addCommand({
      callback: () => void this.#showConflicts(),
      id: "review-conflicts",
      name: "Review workspace conflicts",
    });
    this.addCommand({
      callback: () => void this.#manageExclusions(),
      id: "manage-exclusions",
      name: "Manage semantic exclusions",
    });
    this.addCommand({
      callback: () => void this.#configureProvider(),
      id: "configure-provider",
      name: "Configure semantic provider",
    });
    this.addCommand({
      callback: () => void this.#removeProvider(),
      id: "remove-provider",
      name: "Remove semantic provider access",
    });
    this.addCommand({
      callback: () => void this.#togglePause(),
      id: "toggle-inference-pause",
      name: "Pause or resume automatic inference",
    });
  }

  #registerVaultEvents(): void {
    const changed = (file: TFile): void => {
      if (file.extension.toLocaleLowerCase() === "md") this.#scheduler?.hint();
    };
    this.registerEvent(this.app.vault.on("create", (file) => {
      if (file instanceof TFile) changed(file);
    }));
    this.registerEvent(this.app.vault.on("modify", (file) => {
      if (file instanceof TFile) changed(file);
    }));
    this.registerEvent(this.app.vault.on("delete", (file) => {
      if (file instanceof TFile) changed(file);
    }));
    this.registerEvent(this.app.vault.on("rename", (file) => {
      if (file instanceof TFile) changed(file);
    }));
  }

  async #connectOnStartup(): Promise<void> {
    try {
      const bridge = await this.#bridgeClient();
      const status = parseWorkspaceStatus(await bridge.invoke("workspace.status", {}));
      if (status !== null && (await this.#sameVault(status.vault_path))) {
        this.#scheduler?.hint();
      }
    } catch {
      // Commands surface setup and discovery failures when the user invokes them.
    }
  }

  async #initialize(): Promise<void> {
    try {
      const bridge = await this.#bridgeClient();
      await bridge.invoke("brain.initialize", {});
      const setup = record(await bridge.invoke("workspace.setup", {}, 30_000));
      if (typeof setup.vault_path !== "string") throw new BridgeError("protocol_error");
      if (await this.#sameVault(setup.vault_path)) {
        await this.#scheduler?.manual();
      } else {
        new Notice(`Open the managed Open Brain vault: ${setup.vault_path}`, 12_000);
      }
    } catch (error) {
      this.#noticeError(error);
    }
  }

  async #capture(): Promise<void> {
    const text = await new TextPromptModal(this.app, "Capture to Open Brain", true).result();
    if (text === null) return;
    try {
      const bridge = await this.#bridgeClient();
      await captureText(
        bridge, text,
        (path) => this.#sameVault(path),
        async () => this.#scheduler?.manual(),
      );
      new Notice("Captured to the Open Brain inbox. Review is required before it appears as a vault note.");
      const review = await new ConfirmModal(
        this.app,
        "Review captured material",
        "Review inbox captures for publication in the managed vault now?",
        "Review captures",
      ).result();
      if (review) await this.reviewCaptures();
    } catch (error) {
      this.#noticeError(error);
    }
  }

  public async reviewCaptures(overrides: ReviewCommandOverrides = {}): Promise<void> {
    try {
      const bridge = overrides.bridge ?? await this.#bridgeClient();
      await bridge.invoke("brain.initialize", {});
      const capabilities = overrides.capabilities ?? await this.#clientOperations(bridge);
      requireCapabilities(capabilities, [
        "inbox.list", "inbox.route", "publication.approve", "publication.edit_and_approve",
        "publication.propose", "publication.reject", "publication.show", "space.create",
        "space.list", "workspace.refresh", "workspace.setup", "workspace.status",
      ]);
      const workspace = await preparePublicationWorkspace(
        bridge,
        overrides.sameVault ?? ((path) => this.#sameVault(path)),
      );
      if (!workspace.ready) {
        new Notice(`Open the managed Open Brain vault before reviewing captures: ${workspace.vault_path}`, 12_000);
        return;
      }
      const captures = await listInbox(bridge);
      if (captures.length === 0) {
        new Notice("The Open Brain inbox has no captures to review.");
        return;
      }
      const selected = await new MultiCaptureModal(this.app, captures).result();
      if (selected === null) return;
      const existingSpaces = [...new Set(selected.map((item) => item.space_id).filter((id): id is string => id !== null))];
      if (existingSpaces.length > 1) throw new BridgeError("mixed_source_spaces");
      let spaceId = existingSpaces[0];
      if (spaceId === undefined) {
        const spaces = await listSpaces(bridge);
        const choice = await new ChoiceModal<SpaceItem | "create">(this.app, [
          ...spaces.map((space) => ({ label: space.name, value: space })),
          { label: "Create a new space", value: "create" },
        ], "Choose one publication space").result();
        if (choice === null) return;
        if (choice === "create") {
          const name = await new TextPromptModal(this.app, "New space name", false).result();
          if (name === null) return;
          spaceId = (await createSpace(bridge, name)).space_id;
        } else {
          spaceId = choice.space_id;
        }
      }
      await routeCaptures(bridge, selected, spaceId);
      const initial = deterministicDraft(selected);
      const title = await new TextPromptModal(this.app, "Publication title", false, false, initial.title).result();
      if (title === null) return;
      const markdown = await new TextPromptModal(this.app, "Edit publication draft", true, false, initial.markdown).result();
      if (markdown === null) return;
      let inspection = await proposePublication(bridge, selected, title, markdown);
      while (true) {
        const action = await new PublicationReviewModal(this.app, inspection).result();
        if (action === null) return;
        let edited: string | undefined;
        if (action === "edit_and_approve") {
          const value = await new TextPromptModal(this.app, "Edit and approve publication", true, false, inspection.markdown).result();
          if (value === null) return;
          edited = value;
        }
        try {
          const result = await decidePublication(bridge, inspection, action, edited);
          if (result === null) {
            new Notice("Publication proposal rejected. No vault note was created.");
            return;
          }
          try {
            const note = await refreshAndFindApprovedNote(bridge, result.page_id);
            await this.openManagedFile(note.relative_path);
          } catch (error) {
            const code = error instanceof Error ? error.message : "operation_failed";
            new Notice(`Publication approved, but the managed note could not be refreshed or opened (${code}).`, 10_000);
          }
          return;
        } catch (error) {
          if (!(error instanceof BridgeError) || error.code !== "review_conflict") throw error;
          new Notice("The proposal changed. Inspect the refreshed evidence before deciding again.");
          inspection = await inspectPublication(bridge, inspection.proposal_id);
        }
      }
    } catch (error) {
      this.#noticeError(error);
    }
  }

  async #searchRecords(): Promise<void> {
    try {
      const bridge = await this.#bridgeClient();
      await bridge.invoke("brain.initialize", {});
      const capabilities = await this.#clientOperations(bridge);
      requireCapabilities(capabilities, ["search.page", "record.read"]);
      const request = await new SearchFiltersModal(this.app).result();
      if (request === null) return;
      new PagedRecordSearchModal(this.app, bridge, request).open();
    } catch (error) {
      this.#noticeError(error);
    }
  }

  async #search(): Promise<void> {
    const query = await new TextPromptModal(this.app, "Search Open Brain", false).result();
    if (query === null) return;
    try {
      const bridge = await this.#bridgeClient();
      const response = parseSearch(
        await bridge.invoke("search.query", { limit: 25, query }, 30_000),
      );
      if (response.results.length === 0) {
        new Notice("No Open Brain results found.");
        return;
      }
      new SearchPickerModal(this.app, response.results, (item) => {
        if (item.relative_path === undefined) {
          new Notice("This result is not materialized in the managed vault.");
          return;
        }
        void this.openManagedFile(item.relative_path).catch((error: unknown) => {
          this.#noticeError(error);
        });
      }).open();
    } catch (error) {
      this.#noticeError(error);
    }
  }

  async #showSuggestions(): Promise<void> {
    try {
      const bridge = await this.#managedBridge();
      const response = parseSuggestions(await bridge.invoke("graph.suggestions", {}));
      if (response.suggestions.length === 0) {
        new Notice("No graph suggestions are waiting for review.");
        return;
      }
      new SuggestionPickerModal(this.app, response.suggestions, (suggestion) => {
        void this.reviewSuggestion(suggestion.suggestion_id);
      }).open();
    } catch (error) {
      this.#noticeError(error);
    }
  }

  async #showConflicts(): Promise<void> {
    try {
      const bridge = await this.#managedBridge();
      const response = parseConflicts(await bridge.invoke("workspace.conflicts", {}));
      if (response.conflicts.length === 0) {
        new Notice("No workspace conflicts are waiting for review.");
        return;
      }
      new ConflictPickerModal(this.app, response.conflicts, (summary) => {
        void bridge
          .invoke("workspace.conflict_review", { note_id: summary.note_id })
          .then((value) => {
            const review = parseConflictReview(value);
            if (review.conflict_id !== summary.conflict_id) {
              throw new BridgeError("operation_conflict");
            }
            new ConflictReviewModal(this.app, this, review).open();
          })
          .catch((error: unknown) => this.#noticeError(error));
      }).open();
    } catch (error) {
      this.#noticeError(error);
    }
  }

  async #manageExclusions(): Promise<void> {
    try {
      const bridge = await this.#managedBridge();
      const response = parseExclusions(await bridge.invoke("policy.exclusions", {}));
      const choices: Choice<ExclusionAction>[] = response.exclusions.map((exclusion) => ({
        label: `Include ${exclusion.kind}: ${exclusion.relative_path}`,
        value: { ...exclusion, excluded: false },
      }));
      const current = this.app.workspace.getActiveFile();
      if (current instanceof TFile && current.extension.toLocaleLowerCase() === "md") {
        this.#addExclusionChoice(choices, response.exclusions, {
          excluded: true,
          kind: "note",
          relative_path: current.path,
        });
        const folder = current.parent?.path;
        if (safeRelativePath(folder)) {
          this.#addExclusionChoice(choices, response.exclusions, {
            excluded: true,
            kind: "folder",
            relative_path: folder,
          });
        }
      }
      if (choices.length === 0) {
        new Notice("Open a managed note to exclude it, or add an exclusion first.");
        return;
      }
      const selected = await new ChoiceModal(
        this.app,
        choices,
        "Choose semantic exclusion change",
      ).result();
      if (selected === null) return;
      const confirmed = await new ConfirmModal(
        this.app,
        selected.excluded ? "Exclude from cloud inference" : "Restore cloud eligibility",
        selected.excluded
          ? `${selected.relative_path} will be removed from structural and semantic graph sources before any provider request.`
          : `${selected.relative_path} will be eligible for structural and semantic graph sources, subject to the other privacy rules.`,
        selected.excluded ? "Exclude" : "Include",
      ).result();
      if (!confirmed) return;
      const result = record(
        await bridge.invoke(
          "policy.set_exclusion",
          {
            excluded: selected.excluded,
            kind: selected.kind,
            relative_path: selected.relative_path,
          },
          30_000,
        ),
      );
      if (result.status !== "updated" || result.excluded !== selected.excluded) {
        throw new BridgeError("protocol_error");
      }
      new Notice(
        `${selected.kind === "note" ? "Note" : "Folder"} ${selected.excluded ? "excluded" : "included"}.`,
      );
      await this.refreshPresentation("manual");
    } catch (error) {
      this.#noticeError(error);
    }
  }

  #addExclusionChoice(
    choices: Choice<ExclusionAction>[],
    existing: Exclusion[],
    action: ExclusionAction,
  ): void {
    if (
      existing.some(
        (item) =>
          item.kind === action.kind && item.relative_path === action.relative_path,
      )
    ) {
      return;
    }
    choices.unshift({
      label: `Exclude ${action.kind}: ${action.relative_path}`,
      value: action,
    });
  }

  async #configureProvider(): Promise<void> {
    try {
      const bridge = await this.#managedBridge();
      const status = parseProviderStatus(await bridge.invoke("provider.status", {}));
      const option = await new ChoiceModal(
        this.app,
        status.providers.map((provider) => ({
          label: `${PROVIDER_LABELS[provider.provider]}${provider.status === "blocked" ? " (unavailable)" : ""}`,
          value: provider,
        })),
        "Choose semantic provider access",
      ).result();
      if (option === null) return;
      if (option.provider === "claude_subscription" || option.status === "blocked") {
        new Notice(
          "Claude subscription access remains unavailable because its unprivileged client isolation is not proven.",
          12_000,
        );
        return;
      }
      const custodyChoices: Choice<"os" | "session">[] = [
        { label: "Use for this Obsidian session only", value: "session" },
      ];
      if (status.os_store !== "unavailable") {
        custodyChoices.unshift({
          label:
            status.os_store === "macos_keychain"
              ? "Save in macOS Keychain"
              : "Save in Linux Secret Service",
          value: "os",
        });
      }
      const custody = await new ChoiceModal(
        this.app,
        custodyChoices,
        "Choose API-key storage",
      ).result();
      if (custody === null) return;
      const confirmed = await new ConfirmModal(
        this.app,
        "Allow cloud inference",
        "The selected provider may receive all eligible accepted notes in this managed vault, including future notes. Excluded, inactive, conflicting, secret, and generated content stays out.",
        "Allow eligible notes",
      ).result();
      if (!confirmed) return;

      let credential: string | null = null;
      const saved = custody === "os" && option.credential_saved === true;
      if (saved) {
        const action = await new ChoiceModal(
          this.app,
          [
            { label: "Reuse saved API key", value: "reuse" },
            { label: "Replace saved API key", value: "replace" },
          ] as Choice<"reuse" | "replace">[],
          "Saved API key found",
        ).result();
        if (action === null) return;
        if (action === "replace") {
          const replacement = await new TextPromptModal(
            this.app,
            "Enter replacement API key",
            false,
            true,
          ).result();
          if (replacement === null) return;
          credential = replacement;
        }
      } else {
        credential = await new TextPromptModal(
          this.app,
          "Enter API key",
          false,
          true,
        ).result();
      }
      if (!saved && credential === null) return;
      const configured = record(
        await bridge.invoke(
          "provider.configure",
          {
            credential,
            custody,
            provider: option.provider,
            scope_ack: true,
          },
          30_000,
        ),
      );
      if (configured.status !== "configured") throw new BridgeError("protocol_error");
      this.#providerSelection = {
        access_mode: "api_key",
        custody,
        provider: option.provider,
      };
      new Notice(`${PROVIDER_LABELS[option.provider]} configured.`);
      await this.#scheduler?.manual();
    } catch (error) {
      this.#noticeError(error);
    }
  }

  async #removeProvider(): Promise<void> {
    try {
      const bridge = await this.#managedBridge();
      const status = parseProviderStatus(await bridge.invoke("provider.status", {}));
      const choices = removalChoices(status);
      if (choices.length === 0) {
        new Notice("No Open Brain provider access is configured in this session or OS store.");
        return;
      }
      const selected = await new ChoiceModal(
        this.app,
        choices,
        "Choose provider access to remove",
      ).result();
      if (selected === null) return;
      const confirmed = await new ConfirmModal(
        this.app,
        "Remove provider access",
        `Revoke Open Brain cloud consent for ${PROVIDER_LABELS[selected.provider]} and remove its ${selected.custody === "os" ? "saved" : "session"} key?`,
        "Remove access",
      ).result();
      if (!confirmed) return;
      const removed = record(
        await bridge.invoke(
          "provider.remove",
          { custody: selected.custody, provider: selected.provider },
          30_000,
        ),
      );
      if (removed.status !== "removed") throw new BridgeError("protocol_error");
      if (
        this.#providerSelection?.provider === selected.provider &&
        this.#providerSelection.custody === selected.custody
      ) {
        this.#providerSelection = null;
      }
      new Notice(`${PROVIDER_LABELS[selected.provider]} access removed.`);
    } catch (error) {
      this.#noticeError(error);
    }
  }

  async #togglePause(): Promise<void> {
    await this.updateSettings({ inferencePaused: !this.settings.inferencePaused });
    new Notice(
      this.settings.inferencePaused
        ? "Automatic Open Brain inference is paused. Reconciliation remains active."
        : "Automatic Open Brain inference is enabled.",
    );
  }

  async #bridgeClient(): Promise<OpenBrainBridge> {
    if (this.#bridge !== null) return this.#bridge;
    const executable = await discoverOpenBrainExecutable(this.settings.executablePath.trim());
    const bridge = new OpenBrainBridge(executable);
    try {
      const handshake = parseHandshake(await bridge.invoke("system.handshake", {}));
      if (!handshake.operations.includes("graph.review")) {
        throw new BridgeError("incompatible_binary");
      }
      this.#handshake = handshake;
    } catch (error) {
      bridge.dispose();
      throw error;
    }
    this.#bridge = bridge;
    return bridge;
  }

  async #clientOperations(bridge: OpenBrainBridge): Promise<Set<string>> {
    if (this.#handshake === null) throw new BridgeError("incompatible_binary");
    return await clientCapabilities(bridge, this.#handshake);
  }

  async #managedBridge(): Promise<OpenBrainBridge> {
    const bridge = await this.#bridgeClient();
    const status = parseWorkspaceStatus(await bridge.invoke("workspace.status", {}));
    if (status === null) throw new BridgeError("workspace_unconfigured");
    if (!(await this.#sameVault(status.vault_path))) throw new BridgeError("wrong_vault");
    return bridge;
  }

  async #sameVault(expected: string): Promise<boolean> {
    const adapter = this.app.vault.adapter;
    if (!(adapter instanceof FileSystemAdapter)) return false;
    try {
      return (await realpath(adapter.getBasePath())) === (await realpath(expected));
    } catch {
      return false;
    }
  }

  async #writeCanvas(canvas: Record<string, unknown>): Promise<void> {
    const payload = `${JSON.stringify(canvas, null, 2)}\n`;
    const current = this.app.vault.getAbstractFileByPath(CANVAS_PATH);
    let file: TFile;
    if (current === null) {
      file = await this.app.vault.create(CANVAS_PATH, payload);
    } else {
      if (!(current instanceof TFile) || !(await this.#isOwnedCanvas(current))) {
        throw new BridgeError("canvas_path_owned_by_user");
      }
      await this.app.vault.process(current, () => payload);
      file = current;
    }
    await this.app.workspace.getLeaf("tab").openFile(file);
  }

  async #isOwnedCanvas(file: TFile): Promise<boolean> {
    try {
      const value = record(JSON.parse(await this.app.vault.read(file)));
      if (!Array.isArray(value.nodes)) return false;
      return value.nodes.some((node) => {
        try {
          const item = record(node);
          return typeof item.text === "string" && item.text.startsWith(OWNED_CANVAS_HEADING);
        } catch {
          return false;
        }
      });
    } catch {
      return false;
    }
  }

  #noticeError(error: unknown): void {
    const code = error instanceof Error ? error.message : "operation_failed";
    const messages: Record<string, string> = {
      binary_unavailable:
        "Open Brain is not installed in a supported Homebrew location. Set an absolute path in plugin settings if needed.",
      bridge_closed: "The Open Brain bridge closed. Retry the command.",
      canvas_path_owned_by_user:
        "Open Brain Graph.canvas already exists and is not plugin-owned. Rename it before refreshing.",
      database_busy: "Open Brain is busy. Retry after the current operation finishes.",
      incompatible_binary: "The installed Open Brain binary does not support this plugin version.",
      unsupported_capability: "This Open Brain session lacks the grants required for that workflow. Enable the requested capability and reconnect.",
      cursor_stale: "Search results changed. Restart the search to continue without mixing pages.",
      review_conflict: "The proposal changed. Inspect it again before deciding.",
      mixed_source_spaces: "Selected captures must belong to one space. Choose captures from one space and retry.",
      invalid_source_path: "Open Brain returned an invalid source path.",
      invalid_policy: "The exclusion is no longer valid. Refresh the managed vault.",
      operation_conflict: "This conflict is no longer current. Reopen conflict review.",
      source_unavailable: "The selected source note is no longer available.",
      stale_suggestion: "This suggestion is stale. Refresh the graph before accepting it.",
      target_changed:
        "The note changed while this conflict was open. Reopen conflict review before resolving it.",
      unknown_note: "The selected note is not managed by Open Brain.",
      timeout: "The Open Brain operation timed out and was cancelled.",
      wrong_vault: "This command works only inside the managed Open Brain vault.",
      workspace_unconfigured: "Initialize the managed Open Brain vault first.",
    };
    new Notice(messages[code] ?? `Open Brain could not complete the operation (${code}).`, 10_000);
  }
}

interface Choice<T> {
  label: string;
  value: T;
}

interface ExclusionAction extends Exclusion {
  excluded: boolean;
}

class ChoiceModal<T> extends FuzzySuggestModal<Choice<T>> {
  readonly #items: Choice<T>[];
  #resolve: ((value: T | null) => void) | null = null;
  #settled = false;

  public constructor(app: App, items: Choice<T>[], placeholder: string) {
    super(app);
    this.#items = items;
    this.setPlaceholder(placeholder);
  }

  public result(): Promise<T | null> {
    return new Promise((resolve) => {
      this.#resolve = resolve;
      this.open();
    });
  }

  public override getItems(): Choice<T>[] {
    return this.#items;
  }

  public override getItemText(item: Choice<T>): string {
    return item.label;
  }

  public override onChooseItem(item: Choice<T>): void {
    if (this.#settled) return;
    this.#settled = true;
    this.#resolve?.(item.value);
  }

  public override onClose(): void {
    // Obsidian closes suggestions before calling onChooseItem in the same turn.
    queueMicrotask(() => {
      if (this.#settled) return;
      this.#settled = true;
      this.#resolve?.(null);
    });
  }
}

class ConfirmModal extends Modal {
  readonly #heading: string;
  readonly #message: string;
  readonly #confirmLabel: string;
  #resolve: ((value: boolean) => void) | null = null;
  #settled = false;

  public constructor(
    app: App,
    heading: string,
    message: string,
    confirmLabel: string,
  ) {
    super(app);
    this.#heading = heading;
    this.#message = message;
    this.#confirmLabel = confirmLabel;
  }

  public result(): Promise<boolean> {
    return new Promise((resolve) => {
      this.#resolve = resolve;
      this.open();
    });
  }

  public override onOpen(): void {
    this.titleEl.setText(this.#heading);
    this.contentEl.createEl("p", { text: this.#message });
    const actions = this.contentEl.createDiv({ cls: "open-brain-review__actions" });
    const cancel = actions.createEl("button", { text: "Cancel" });
    cancel.addEventListener("click", () => this.close());
    const confirm = actions.createEl("button", { text: this.#confirmLabel });
    confirm.addClass("mod-cta");
    confirm.addEventListener("click", () => {
      this.#settled = true;
      this.#resolve?.(true);
      this.close();
    });
  }

  public override onClose(): void {
    this.contentEl.empty();
    if (!this.#settled) this.#resolve?.(false);
  }
}

class TextPromptModal extends Modal {
  readonly #title: string;
  readonly #multiline: boolean;
  readonly #secret: boolean;
  readonly #initial: string;
  #resolve: ((value: string | null) => void) | null = null;
  #settled = false;

  public constructor(app: App, title: string, multiline: boolean, secret = false, initial = "") {
    super(app);
    this.#title = title;
    this.#multiline = multiline;
    this.#secret = secret;
    this.#initial = initial;
  }

  public result(): Promise<string | null> {
    return new Promise((resolve) => {
      this.#resolve = resolve;
      this.open();
    });
  }

  public override onOpen(): void {
    this.titleEl.setText(this.#title);
    const field = this.#multiline
      ? this.contentEl.createEl("textarea", { attr: { rows: "8" } })
      : this.contentEl.createEl("input", {
          attr: {
            autocomplete: this.#secret ? "new-password" : "off",
            type: this.#secret ? "password" : "text",
          },
        });
    field.addClass("open-brain-input");
    field.value = this.#initial;
    const submit = this.contentEl.createEl("button", { text: "Continue" });
    submit.addClass("mod-cta");
    submit.addEventListener("click", () => {
      const value = field.value.trim();
      if (value.length === 0) return;
      this.#settled = true;
      this.#resolve?.(value);
      this.close();
    });
    field.focus();
  }

  public override onClose(): void {
    this.contentEl.empty();
    if (!this.#settled) this.#resolve?.(null);
  }
}

class SearchPickerModal extends FuzzySuggestModal<SearchItem> {
  readonly #items: SearchItem[];
  readonly #chosen: (item: SearchItem) => void;

  public constructor(app: App, items: SearchItem[], chosen: (item: SearchItem) => void) {
    super(app);
    this.#items = items;
    this.#chosen = chosen;
    this.setPlaceholder("Choose an Open Brain result");
  }

  public override getItems(): SearchItem[] {
    return this.#items;
  }

  public override getItemText(item: SearchItem): string {
    return `${item.title}: ${item.excerpt}`;
  }

  public override onChooseItem(item: SearchItem): void {
    this.#chosen(item);
  }
}

export class MultiCaptureModal extends Modal {
  readonly #items: InboxItem[];
  #resolve: ((value: InboxItem[] | null) => void) | null = null;
  #settled = false;

  public constructor(app: App, items: InboxItem[]) {
    super(app);
    this.#items = items;
  }

  public result(): Promise<InboxItem[] | null> {
    return new Promise((resolve) => { this.#resolve = resolve; this.open(); });
  }

  public override onOpen(): void {
    this.titleEl.setText("Select 1–32 captures for publication");
    const selected = new Set<string>();
    for (const item of this.#items) {
      const row = this.contentEl.createEl("label", { cls: "open-brain-capture-choice" });
      const checkbox = row.createEl("input", { attr: { type: "checkbox" } });
      row.createSpan({ text: ` ${displayProjection(item.title ?? item.preview)}` });
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          if (selected.size >= 32) { checkbox.checked = false; return; }
          selected.add(item.capture_id);
        } else selected.delete(item.capture_id);
      });
    }
    const submit = this.contentEl.createEl("button", { text: "Create draft" });
    submit.addClass("mod-cta");
    submit.addEventListener("click", () => {
      const items = this.#items.filter((item) => selected.has(item.capture_id));
      if (items.length === 0) { new Notice("Select at least one capture."); return; }
      this.#settled = true;
      this.#resolve?.(items);
      this.close();
    });
  }

  public override onClose(): void {
    this.contentEl.empty();
    if (!this.#settled) this.#resolve?.(null);
  }
}

function displayProjection(value: string): string {
  return value.replace(DISPLAY_CONTROL, (character) =>
    `\\u${character.codePointAt(0)!.toString(16).padStart(4, "0")}`);
}

type PublicationAction = "approve" | "edit_and_approve" | "reject";

export class PublicationReviewModal extends Modal {
  readonly #inspection: PublicationInspection;
  #resolve: ((value: PublicationAction | null) => void) | null = null;
  #settled = false;

  public constructor(app: App, inspection: PublicationInspection) {
    super(app);
    this.#inspection = inspection;
  }

  public result(): Promise<PublicationAction | null> {
    return new Promise((resolve) => { this.#resolve = resolve; this.open(); });
  }

  public override onOpen(): void {
    this.titleEl.setText("Inspect publication and evidence");
    this.contentEl.createEl("h3", { text: this.#inspection.title });
    this.contentEl.createEl("pre", { cls: "open-brain-publication-draft", text: this.#inspection.markdown });
    this.contentEl.createEl("h4", { text: "Evidence" });
    for (const evidence of this.#inspection.evidence) {
      this.contentEl.createEl("blockquote", { text: evidence.excerpt });
    }
    const actions = this.contentEl.createDiv({ cls: "open-brain-review__actions" });
    this.#action(actions, "Approve", "approve", true);
    this.#action(actions, "Edit and approve", "edit_and_approve", false);
    this.#action(actions, "Reject", "reject", false);
  }

  public override onClose(): void {
    this.contentEl.empty();
    if (!this.#settled) this.#resolve?.(null);
  }

  #action(parent: HTMLElement, label: string, action: PublicationAction, primary: boolean): void {
    const button = parent.createEl("button", { text: label });
    if (primary) button.addClass("mod-cta");
    button.addEventListener("click", () => {
      this.#settled = true;
      this.#resolve?.(action);
      this.close();
    });
  }
}

export class SearchFiltersModal extends Modal {
  #resolve: ((value: SearchRequest | null) => void) | null = null;
  #settled = false;

  public result(): Promise<SearchRequest | null> {
    return new Promise((resolve) => { this.#resolve = resolve; this.open(); });
  }

  public override onOpen(): void {
    this.titleEl.setText("Search Open Brain records");
    const query = this.#field("Query");
    const spaces = this.#field("Space IDs (comma separated, optional)");
    const payloads = this.#field("Payload families (comma separated, optional)");
    const types = this.#field("Record types: source, canonical (optional)");
    this.contentEl.createEl("label", { text: "Search mode" });
    const mode = this.contentEl.createEl("select");
    mode.createEl("option", { attr: { value: "lexical" }, text: "Lexical" });
    mode.createEl("option", { attr: { value: "hybrid_preferred" }, text: "Hybrid (when available)" });
    const submit = this.contentEl.createEl("button", { text: "Search" });
    submit.addClass("mod-cta");
    submit.addEventListener("click", () => {
      const value = query.value.trim();
      if (value.length === 0) return;
      const recordTypes = splitFilter(types.value);
      if (recordTypes.some((type) => type !== "source" && type !== "canonical")) {
        new Notice("Record types must be source or canonical.");
        return;
      }
      this.#settled = true;
      this.#resolve?.({ cursor: null, filters: { payload_families: splitFilter(payloads.value),
        record_types: recordTypes, space_ids: splitFilter(spaces.value) }, limit: 50,
        mode: mode.value === "hybrid_preferred" ? "hybrid_preferred" : "lexical", query: value });
      this.close();
    });
    query.focus();
  }

  public override onClose(): void {
    this.contentEl.empty();
    if (!this.#settled) this.#resolve?.(null);
  }

  #field(label: string): HTMLInputElement {
    this.contentEl.createEl("label", { text: label });
    const field = this.contentEl.createEl("input", { attr: { autocomplete: "off", type: "text" } });
    field.addClass("open-brain-input");
    return field;
  }
}

export class PagedRecordSearchModal extends Modal {
  readonly #bridge: Pick<OpenBrainBridge, "invoke" | "cancelPending">;
  readonly #base: SearchRequest;
  #active = true;
  #cursor: string | null;
  #loading = false;
  #results: HTMLElement | null = null;
  #status: HTMLElement | null = null;
  #controls: HTMLElement | null = null;
  #readButtons = new Set<HTMLButtonElement>();
  #readEpoch = 0;
  #reading = false;

  public constructor(app: App, bridge: Pick<OpenBrainBridge, "invoke" | "cancelPending">, request: SearchRequest) {
    super(app);
    this.#bridge = bridge;
    this.#base = request;
    this.#cursor = request.cursor;
  }

  public override onOpen(): void {
    this.titleEl.setText("Open Brain search results");
    this.#status = this.contentEl.createEl("p");
    this.#results = this.contentEl.createDiv({ cls: "open-brain-search-results" });
    this.#controls = this.contentEl.createDiv({ cls: "open-brain-review__actions" });
    void this.#load(false);
  }

  public override onClose(): void {
    this.#active = false;
    this.#readEpoch += 1;
    this.#bridge.cancelPending();
    this.contentEl.empty();
  }

  async #load(restart: boolean): Promise<void> {
    if (!this.#active || this.#loading) return;
    this.#loading = true;
    if (restart) { this.#cursor = null; this.#results?.empty(); }
    this.#controls?.empty();
    this.#status?.setText("Loading bounded results…");
    try {
      const page = await searchPage(this.#bridge, { ...this.#base, cursor: this.#cursor });
      if (!this.#active) return;
      this.#status?.setText(`Mode: ${page.mode_used}${page.warnings.length > 0 ? `. Warnings: ${page.warnings.join(", ")}` : ""}`);
      for (const item of page.results) this.#append(item);
      this.#cursor = page.next_cursor;
      if (page.next_cursor !== null) this.#button("Load more", () => this.#load(false));
      else if (this.#results?.childElementCount === 0) this.#status?.setText("No Open Brain results found.");
    } catch (error) {
      if (!this.#active) return;
      const code = error instanceof Error ? error.message : "operation_failed";
      if (code === "cursor_stale") {
        this.#status?.setText("Results changed. Restart to replace these pages while preserving the query and filters.");
        this.#button("Restart search", () => this.#load(true));
      } else {
        this.#status?.setText(`Search stopped (${code}).`);
      }
    } finally {
      this.#loading = false;
    }
  }

  #append(item: RecordSummary): void {
    const row = this.#results?.createDiv({ cls: "open-brain-search-result" });
    if (row === undefined) return;
    row.createEl("h4", { text: item.title });
    row.createEl("p", { text: `${item.record_type} · revision ${item.revision_id}` });
    row.createEl("blockquote", { text: item.excerpt });
    const read = row.createEl("button", { text: "Read complete record" });
    this.#readButtons.add(read);
    read.addEventListener("click", () => {
      if (!this.#active || this.#reading) return;
      this.#reading = true;
      const epoch = ++this.#readEpoch;
      for (const button of this.#readButtons) button.disabled = true;
      void readCompleteRecord(this.#bridge, item.record_id, item.revision_id, () => !this.#active || epoch !== this.#readEpoch)
        .then((content) => { if (this.#active) new FullRecordModal(this.app, item, content).open(); })
        .catch((error: unknown) => {
          const code = error instanceof Error ? error.message : "operation_failed";
          if (this.#active && code !== "cancelled") new Notice(`Open Brain read stopped (${code}).`);
        })
        .finally(() => {
          if (this.#active && epoch === this.#readEpoch) {
            this.#reading = false;
            for (const button of this.#readButtons) button.disabled = false;
          }
        });
    });
  }

  #button(label: string, action: () => Promise<void>): void {
    const button = this.#controls?.createEl("button", { text: label });
    button?.addEventListener("click", () => { void action(); });
  }
}

class FullRecordModal extends Modal {
  readonly #item: RecordSummary;
  readonly #content: string;
  public constructor(app: App, item: RecordSummary, content: string) { super(app); this.#item = item; this.#content = content; }
  public override onOpen(): void {
    this.titleEl.setText(this.#item.title);
    this.contentEl.createEl("p", { text: `${this.#item.record_type} · revision ${this.#item.revision_id}` });
    this.contentEl.createEl("pre", { cls: "open-brain-record-content", text: this.#content });
  }
  public override onClose(): void { this.contentEl.empty(); }
}

function splitFilter(value: string): string[] {
  return [...new Set(value.split(",").map((part) => part.trim()).filter(Boolean))];
}

class SuggestionPickerModal extends FuzzySuggestModal<SuggestionSummary> {
  readonly #items: SuggestionSummary[];
  readonly #chosen: (item: SuggestionSummary) => void;

  public constructor(
    app: App,
    items: SuggestionSummary[],
    chosen: (item: SuggestionSummary) => void,
  ) {
    super(app);
    this.#items = items;
    this.#chosen = chosen;
    this.setPlaceholder("Choose a graph suggestion to review");
  }

  public override getItems(): SuggestionSummary[] {
    return this.#items;
  }

  public override getItemText(item: SuggestionSummary): string {
    return `${item.source_quote} → ${item.target_quote} (${item.revision_status})`;
  }

  public override onChooseItem(item: SuggestionSummary): void {
    this.#chosen(item);
  }
}

class ConflictPickerModal extends FuzzySuggestModal<ConflictSummary> {
  readonly #items: ConflictSummary[];
  readonly #chosen: (item: ConflictSummary) => void;

  public constructor(
    app: App,
    items: ConflictSummary[],
    chosen: (item: ConflictSummary) => void,
  ) {
    super(app);
    this.#items = items;
    this.#chosen = chosen;
    this.setPlaceholder("Choose a workspace conflict to review");
  }

  public override getItems(): ConflictSummary[] {
    return this.#items;
  }

  public override getItemText(item: ConflictSummary): string {
    return item.relative_path;
  }

  public override onChooseItem(item: ConflictSummary): void {
    this.#chosen(item);
  }
}

class ConflictReviewModal extends Modal {
  readonly #plugin: OpenBrainPlugin;
  readonly #review: ConflictReview;

  public constructor(app: App, plugin: OpenBrainPlugin, review: ConflictReview) {
    super(app);
    this.#plugin = plugin;
    this.#review = review;
  }

  public override onOpen(): void {
    this.titleEl.setText("Resolve Open Brain conflict");
    this.contentEl.addClass("open-brain-review");
    this.contentEl.createEl("p", {
      text: `${this.#review.relative_path} has an accepted Open Brain version and a different vault edit. Choose which complete version to keep.`,
    });
    this.#version("Accepted Open Brain version", this.#review.accepted_body);
    this.#version("Current vault edit", this.#review.workspace_body);
    const actions = this.contentEl.createDiv({ cls: "open-brain-review__actions" });
    this.#resolutionButton(actions, "Keep Open Brain version", "accepted");
    this.#resolutionButton(actions, "Keep vault edit", "workspace");
  }

  public override onClose(): void {
    this.contentEl.empty();
  }

  #version(label: string, body: string): void {
    const section = this.contentEl.createDiv({ cls: "open-brain-conflict__version" });
    section.createEl("h3", { text: label });
    section.createEl("pre", { text: body });
  }

  #resolutionButton(
    parent: HTMLElement,
    label: string,
    choice: "accepted" | "workspace",
  ): void {
    const button = parent.createEl("button", { text: label });
    button.addEventListener("click", () => {
      for (const action of Array.from(parent.querySelectorAll("button"))) {
        action.disabled = true;
      }
      void this.#plugin
        .resolveConflict(this.#review, choice)
        .then(() => this.close())
        .catch(() => {
          for (const action of Array.from(parent.querySelectorAll("button"))) {
            action.disabled = false;
          }
        });
    });
  }
}

class SuggestionReviewModal extends Modal {
  readonly #plugin: OpenBrainPlugin;
  readonly #review: SuggestionReview;

  public constructor(app: App, plugin: OpenBrainPlugin, review: SuggestionReview) {
    super(app);
    this.#plugin = plugin;
    this.#review = review;
  }

  public override onOpen(): void {
    this.titleEl.setText("Review Open Brain connection");
    this.contentEl.addClass("open-brain-review");
    this.contentEl.createEl("p", {
      cls: this.#review.revision_status === "stale" ? "open-brain-review__stale" : "",
      text: `Status: ${this.#review.revision_status}. Provider: ${this.#review.provider}. Model: ${this.#review.model}.`,
    });
    this.contentEl.createEl("blockquote", {
      cls: "open-brain-review__evidence",
      text: `Source evidence: ${this.#review.source.quote}`,
    });
    this.contentEl.createEl("blockquote", {
      cls: "open-brain-review__evidence",
      text: `Target evidence: ${this.#review.target.quote}`,
    });
    this.contentEl.createEl("p", {
      text: `Accepting appends ${this.#review.append_text} to ${this.#review.source.relative_path}.`,
    });
    const actions = this.contentEl.createDiv({ cls: "open-brain-review__actions" });
    this.#button(actions, "Open source", () => this.#plugin.openManagedFile(this.#review.source.relative_path));
    this.#button(actions, "Open target", () => this.#plugin.openManagedFile(this.#review.target.relative_path));
    const accept = actions.createEl("button", { text: "Accept connection" });
    accept.addClass("mod-cta");
    accept.disabled = this.#review.revision_status !== "current";
    accept.addEventListener("click", () => {
      accept.disabled = true;
      void this.#plugin
        .acceptSuggestion(this.#review)
        .then(() => this.close())
        .catch(() => {
          accept.disabled = false;
        });
    });
  }

  public override onClose(): void {
    this.contentEl.empty();
  }

  #button(parent: HTMLElement, label: string, action: () => Promise<void>): void {
    const button = parent.createEl("button", { text: label });
    button.addEventListener("click", () => {
      void action().catch((error: unknown) => {
        const code = error instanceof Error ? error.message : "operation_failed";
        new Notice(`Open Brain could not open the note (${code}).`);
      });
    });
  }
}

function removalChoices(status: ProviderStatus): Choice<ProviderSelection>[] {
  const selections = new Map<string, ProviderSelection>();
  if (status.selected !== null) {
    selections.set(
      `${status.selected.provider}:${status.selected.custody}`,
      status.selected,
    );
  }
  if (status.os_store !== "unavailable") {
    for (const option of status.providers) {
      if (option.provider === "claude_subscription" || option.credential_saved !== true) {
        continue;
      }
      const selection: ProviderSelection = {
        access_mode: "api_key",
        custody: "os",
        provider: option.provider,
      };
      selections.set(`${selection.provider}:${selection.custody}`, selection);
    }
  }
  return [...selections.values()].map((selection) => ({
    label: `${PROVIDER_LABELS[selection.provider]} (${selection.custody === "os" ? "saved key" : "this session"})`,
    value: selection,
  }));
}

function parseSettings(value: unknown): OpenBrainSettings {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return { ...DEFAULT_SETTINGS };
  }
  const loaded = value as Record<string, unknown>;
  return {
    executablePath:
      typeof loaded.executablePath === "string" ? loaded.executablePath : "",
    inferencePaused:
      typeof loaded.inferencePaused === "boolean" ? loaded.inferencePaused : false,
  };
}

class OpenBrainSettingTab extends PluginSettingTab {
  readonly #plugin: OpenBrainPlugin;

  public constructor(app: App, plugin: OpenBrainPlugin) {
    super(app, plugin);
    this.#plugin = plugin;
  }

  public override display(): void {
    this.containerEl.empty();
    new Setting(this.containerEl)
      .setName("Open Brain executable")
      .setDesc(
        "Leave blank for supported Homebrew locations, or set an absolute path. Install with brew install cbolden15/tap/open-brain.",
      )
      .addText((text) => {
        text.setPlaceholder("/opt/homebrew/bin/open-brain");
        text.setValue(this.#plugin.settings.executablePath);
        text.onChange(async (value) => {
          await this.#plugin.updateSettings({ executablePath: value.trim() });
          this.#plugin.resetBridge();
        });
      });
    new Setting(this.containerEl)
      .setName("Pause automatic inference")
      .setDesc(
        "Reconciliation and local structural refresh continue. Manual refresh remains available.",
      )
      .addToggle((toggle) => {
        toggle.setValue(this.#plugin.settings.inferencePaused);
        toggle.onChange(async (value) => {
          await this.#plugin.updateSettings({ inferencePaused: value });
        });
      });
  }
}
