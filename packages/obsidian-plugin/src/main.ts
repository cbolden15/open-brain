import { realpath } from "node:fs/promises";

import {
  App,
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
import {
  type SearchItem,
  type SuggestionReview,
  type SuggestionSummary,
  parseCanvas,
  parseHandshake,
  parseReconcile,
  parseReview,
  parseSearch,
  parseSuggestions,
  parseWorkspaceStatus,
  record,
  safeRelativePath,
} from "./contracts";
import { RefreshScheduler, type RefreshReason } from "./refresh-scheduler";

const CANVAS_PATH = normalizePath("Open Brain Graph.canvas");
const OWNED_CANVAS_HEADING = "# Open Brain graph";

interface OpenBrainSettings {
  executablePath: string;
  inferencePaused: boolean;
}

const DEFAULT_SETTINGS: OpenBrainSettings = {
  executablePath: "",
  inferencePaused: false,
};

export default class OpenBrainPlugin extends Plugin {
  public override settings: OpenBrainSettings = DEFAULT_SETTINGS;
  #bridge: OpenBrainBridge | null = null;
  #scheduler: RefreshScheduler | null = null;

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
    this.#bridge?.dispose();
    this.#bridge = null;
  }

  async updateSettings(update: Partial<OpenBrainSettings>): Promise<void> {
    this.settings = { ...this.settings, ...update };
    await this.saveData(this.settings);
  }

  resetBridge(): void {
    this.#bridge?.dispose();
    this.#bridge = null;
  }

  async refreshPresentation(reason: RefreshReason): Promise<void> {
    try {
      const bridge = await this.#managedBridge();
      const reconciliation = parseReconcile(
        await bridge.invoke("workspace.reconcile", {}, 30_000),
      );
      await bridge.invoke("graph.refresh_structural", {}, 90_000);
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
    const file = this.app.vault.getAbstractFileByPath(normalizePath(relativePath));
    if (!(file instanceof TFile)) throw new BridgeError("source_unavailable");
    await this.app.workspace.getLeaf("tab").openFile(file);
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
      await bridge.invoke("capture.create", { text }, 30_000);
      const status = parseWorkspaceStatus(await bridge.invoke("workspace.status", {}));
      if (status !== null && (await this.#sameVault(status.vault_path))) {
        await bridge.invoke("workspace.refresh", {}, 30_000);
        await this.#scheduler?.manual();
      }
      new Notice("Captured to Open Brain.");
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
    } catch (error) {
      bridge.dispose();
      throw error;
    }
    this.#bridge = bridge;
    return bridge;
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
      invalid_source_path: "Open Brain returned an invalid source path.",
      source_unavailable: "The selected source note is no longer available.",
      stale_suggestion: "This suggestion is stale. Refresh the graph before accepting it.",
      timeout: "The Open Brain operation timed out and was cancelled.",
      wrong_vault: "This command works only inside the managed Open Brain vault.",
      workspace_unconfigured: "Initialize the managed Open Brain vault first.",
    };
    new Notice(messages[code] ?? `Open Brain could not complete the operation (${code}).`, 10_000);
  }
}

class TextPromptModal extends Modal {
  readonly #title: string;
  readonly #multiline: boolean;
  #resolve: ((value: string | null) => void) | null = null;
  #settled = false;

  public constructor(app: App, title: string, multiline: boolean) {
    super(app);
    this.#title = title;
    this.#multiline = multiline;
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
      : this.contentEl.createEl("input", { attr: { type: "text" } });
    field.addClass("open-brain-input");
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
