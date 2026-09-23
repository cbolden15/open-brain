import { chmod, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("obsidian", () => {
  const opened: unknown[] = [];
  const notices: string[] = [];
  class Element {
    public checked = false;
    public children: Element[] = [];
    public disabled = false;
    public events: Record<string, () => void> = {};
    public tag: string;
    public text: string;
    public value = "";
    public constructor(tag = "div", options: { text?: string } = {}) { this.tag = tag; this.text = options.text ?? ""; }
    public addClass(): void {}
    public addEventListener(name: string, callback: () => void): void { this.events[name] = callback; }
    public createDiv(options?: { text?: string }): Element { return this.createEl("div", options); }
    public createEl(tag: string, options?: { text?: string }): Element { const child = new Element(tag, options); this.children.push(child); return child; }
    public createSpan(options?: { text?: string }): Element { return this.createEl("span", options); }
    public empty(): void { this.children = []; }
    public focus(): void {}
    public setText(text: string): void { this.text = text; }
    public trigger(name: string): void { this.events[name]?.(); }
    public get childElementCount(): number { return this.children.length; }
  }
  class Modal {
    public app: unknown;
    public contentEl = new Element();
    public titleEl = new Element();
    public constructor(app: unknown) { this.app = app; }
    public close(): void { (this as { onClose?: () => void }).onClose?.(); }
    public open(): void { opened.push(this); (this as { onOpen?: () => void }).onOpen?.(); }
  }
  class FuzzySuggestModal<T> extends Modal {
    public placeholder = "";
    public setPlaceholder(value: string): void { this.placeholder = value; }
    public getItems(): T[] { return []; }
    public selectSuggestion(item: T): void {
      // Obsidian 1.13.7 closes the modal before invoking the choice callback.
      this.close();
      (this as unknown as { onChooseItem(item: T): void }).onChooseItem(item);
    }
  }
  class FileSystemAdapter {
    public constructor(readonly basePath = "", readonly onGetBasePath = () => {}) {}
    public getBasePath(): string { this.onGetBasePath(); return this.basePath; }
  }
  class TFile { public constructor(readonly path = "") {} }
  return {
    __notices: notices, __opened: opened,
    App: class {}, FileSystemAdapter, FuzzySuggestModal, Modal,
    Notice: class { public constructor(message: string) { notices.push(message); } },
    Plugin: class { public app: unknown; public constructor(app: unknown) { this.app = app; } },
    PluginSettingTab: class {}, Setting: class {}, TFile,
    normalizePath: (value: string) => value,
  };
});

import OpenBrainPlugin, { PagedRecordSearchModal, SearchFiltersModal } from "../src/main";
import { validateT03Wire } from "../src/t03-wire";

afterEach(() => vi.restoreAllMocks());

type TestElement = { children: TestElement[]; disabled: boolean; tag: string; text: string; trigger(name: string): void; value: string };
const walk = (element: TestElement): TestElement[] => [element, ...element.children.flatMap(walk)];
const uuid = (n: number) => `123e4567-e89b-42d3-a456-${n.toString(16).padStart(12, "0")}`;
const summary = (n: number) => {
  const captureId = `capture_${uuid(n)}`;
  return { record_id: captureId, record_type: "source", revision_id: captureId, source_id: `source_${uuid(1000 + n)}`,
    payload_family: "text", space_id: null, title: `Title ${n}`, excerpt: `Excerpt ${n}`, trust: "third_party",
    provenance: { representative_capture_id: captureId, capture_ids: [captureId], source_origin: "third_party" },
    source_update_available: false };
};

describe("actual T07 modals", () => {
  it("maps the visible Hybrid choice to the frozen hybrid_preferred request", async () => {
    const modal = new SearchFiltersModal({} as never);
    const result = modal.result();
    const elements = walk(modal.contentEl as unknown as TestElement);
    elements.find((element) => element.tag === "input")!.value = "synthetic";
    elements.find((element) => element.tag === "select")!.value = "hybrid_preferred";
    elements.find((element) => element.text === "Search")!.trigger("click");
    const request = await result;
    expect(request?.mode).toBe("hybrid_preferred");
    expect(() => validateT03Wire("search.page.request", { dto_version: 1, ...request })).not.toThrow();
  });

  it("close/Escape cancels the held read and prevents another chunk request", async () => {
    let release!: (value: unknown) => void;
    let reads = 0;
    const page = { status: "ok", dto_version: 1, results: [summary(0)], next_cursor: null,
      complete: true, mode_used: "lexical", warnings: [] };
    const bridge = {
      cancelPending: vi.fn(),
      invoke: vi.fn(async (operation: string) => {
        if (operation === "search.page") return page;
        if (operation === "record.read") {
          reads += 1;
          if (reads === 1) return await new Promise((resolve) => { release = resolve; });
          return { status: "ok", dto_version: 1, record: summary(0), content: { kind: "untrusted_text", text: "end" },
            start_byte: 1, end_byte: 4, next_cursor: null, complete: true };
        }
        throw new Error(operation);
      }),
    };
    const modal = new PagedRecordSearchModal({} as never, bridge as never, {
      cursor: null, filters: { payload_families: [], record_types: [], space_ids: [] },
      limit: 50, mode: "lexical", query: "synthetic",
    });
    modal.open();
    await vi.waitFor(() => expect(walk(modal.contentEl as unknown as TestElement).some((item) => item.text === "Read complete record")).toBe(true));
    walk(modal.contentEl as unknown as TestElement).find((item) => item.text === "Read complete record")!.trigger("click");
    await vi.waitFor(() => expect(reads).toBe(1));
    modal.close();
    release({ status: "ok", dto_version: 1, record: summary(0), content: { kind: "untrusted_text", text: "x" },
      start_byte: 0, end_byte: 1, next_cursor: "cursor-1", complete: false });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(reads).toBe(1);
    expect(bridge.cancelPending).toHaveBeenCalledOnce();
  });

  it("allows only one complete-read loop across result rows", async () => {
    let release!: (value: unknown) => void;
    let reads = 0;
    const bridge = {
      cancelPending: vi.fn(),
      invoke: vi.fn(async (operation: string) => {
        if (operation === "search.page") return { status: "ok", dto_version: 1, results: [summary(0), summary(1)],
          next_cursor: null, complete: true, mode_used: "lexical", warnings: [] };
        reads += 1;
        return await new Promise((resolve) => { release = resolve; });
      }),
    };
    const modal = new PagedRecordSearchModal({} as never, bridge as never, {
      cursor: null, filters: { payload_families: [], record_types: [], space_ids: [] },
      limit: 50, mode: "lexical", query: "synthetic",
    });
    modal.open();
    await vi.waitFor(() => expect(walk(modal.contentEl as unknown as TestElement).filter((item) => item.text === "Read complete record")).toHaveLength(2));
    const buttons = walk(modal.contentEl as unknown as TestElement).filter((item) => item.text === "Read complete record");
    buttons[0]!.trigger("click");
    buttons[1]!.trigger("click");
    await vi.waitFor(() => expect(reads).toBe(1));
    modal.close();
    release({});
  });

  it("the actual review command hands off before publication when the managed vault differs", async () => {
    const obsidian = await import("obsidian") as unknown as { __notices: string[] };
    obsidian.__notices.length = 0;
    const operations: string[] = [];
    const bridge = {
      invoke: vi.fn(async (operation: string) => {
        operations.push(operation);
        if (operation === "brain.initialize") return { status: "initialized" };
        if (operation === "workspace.status") return { status: "unconfigured" };
        if (operation === "workspace.setup") return { status: "setup", vault_path: "/synthetic/managed" };
        throw new Error(`unexpected ${operation}`);
      }),
    };
    const plugin = new OpenBrainPlugin({} as never, {} as never);
    await plugin.reviewCaptures({ bridge: bridge as never, capabilities: publicationCapabilities(), sameVault: async () => false });
    expect(operations).toEqual(["brain.initialize", "workspace.status", "workspace.setup"]);
    expect(obsidian.__notices).toContain("Open the managed Open Brain vault before reviewing captures: /synthetic/managed");
  });

  it("the actual review command displays a control-bearing preview across inbox pages", async () => {
    const obsidian = await import("obsidian") as unknown as {
      __notices: string[];
      __opened: Array<{ contentEl: TestElement; titleEl: TestElement }>;
    };
    obsidian.__notices.length = 0;
    obsidian.__opened.length = 0;
    const hostilePreview = "\u001b]52;c;SYNTHETIC_CLIPBOARD\u0007 <script>synthetic()<[protected]> [run](command:synthetic)";
    const capture = (n: number, preview = `Ordinary ${n}`) => ({
      capture_id: `capture_${uuid(n)}`, payload_family: "text", preview, space_id: null, title: null,
    });
    const pages = new Map<number, { items: ReturnType<typeof capture>[]; next_offset: number | null }>([
      [0, { items: [capture(1), capture(2)], next_offset: 50 }],
      [50, { items: [capture(3), capture(4)], next_offset: 100 }],
      [100, { items: [capture(5), capture(6)], next_offset: 150 }],
      [150, { items: [capture(7), capture(8)], next_offset: 200 }],
      [200, { items: [capture(9, hostilePreview), capture(10)], next_offset: null }],
    ]);
    const offsets: number[] = [];
    const bridge = {
      invoke: vi.fn(async (operation: string, arguments_: Record<string, unknown>) => {
        if (operation === "brain.initialize") return { status: "already_initialized" };
        if (operation === "workspace.status") return { status: "ok", vault_path: "/synthetic/managed" };
        if (operation === "inbox.list") {
          const offset = Number(arguments_.offset);
          offsets.push(offset);
          return { status: "listed", offset, ...pages.get(offset)! };
        }
        throw new Error(`unexpected ${operation}`);
      }),
    };
    const plugin = new OpenBrainPlugin({} as never, {} as never);
    const running = plugin.reviewCaptures({
      bridge: bridge as never, capabilities: publicationCapabilities(), sameVault: async () => true,
    });

    const selection = await openedModal(obsidian.__opened, "Select 1–32 captures for publication");
    const labels = walk(selection.contentEl).filter((element) => element.tag === "span").map((element) => element.text);
    expect(offsets).toEqual([0, 50, 100, 150, 200]);
    expect(labels).toContain(" \\u001b]52;c;SYNTHETIC_CLIPBOARD\\u0007 <script>synthetic()<[protected]> [run](command:synthetic)");
    expect(labels.some((label) => label.includes("\u001b") || label.includes("\u0007"))).toBe(false);
    (selection as unknown as { close(): void }).close();
    await running;
    expect(obsidian.__notices).toEqual([]);
  });

  it("opens a managed file after Obsidian registers the externally refreshed note", async () => {
    const vaultPath = await mkdtemp(path.join(tmpdir(), "open-brain-managed-vault-"));
    const executable = await managedBridgeExecutable(vaultPath);
    const relativePath = "spaces/synthetic/notes/page_delayed.md";
    const harness = await managedFileHarness(vaultPath, executable);
    const opening = harness.plugin.openManagedFile(relativePath);
    void opening.catch(() => undefined);

    try {
      await harness.listenerRegistered;
      const file = new harness.TFile(relativePath);
      harness.register(file);
      await opening;
      expect(harness.openFile).toHaveBeenCalledWith(file, { active: true });
      expect(harness.offref).toHaveBeenCalledOnce();
    } finally {
      harness.plugin.onunload();
    }
  });

  it("bounds a genuinely absent managed file and removes its create listener", async () => {
    const nativeSetTimeout = globalThis.setTimeout;
    vi.spyOn(globalThis, "setTimeout").mockImplementation(((callback: () => void, delay?: number) =>
      nativeSetTimeout(callback, delay === 10_000 ? 5 : delay)) as typeof setTimeout);
    const vaultPath = await mkdtemp(path.join(tmpdir(), "open-brain-managed-vault-"));
    const executable = await managedBridgeExecutable(vaultPath);
    const harness = await managedFileHarness(vaultPath, executable);

    try {
      await expect(harness.plugin.openManagedFile("spaces/synthetic/notes/missing.md")).rejects.toEqual(
        expect.objectContaining({ code: "source_unavailable" }),
      );
      expect(harness.offref).toHaveBeenCalledOnce();
      expect(harness.openFile).not.toHaveBeenCalled();
    } finally {
      harness.plugin.onunload();
    }
  });

  it("cancels a pending managed-file wait on unload", async () => {
    const vaultPath = await mkdtemp(path.join(tmpdir(), "open-brain-managed-vault-"));
    const executable = await managedBridgeExecutable(vaultPath);
    const harness = await managedFileHarness(vaultPath, executable);
    const opening = harness.plugin.openManagedFile("spaces/synthetic/notes/pending.md");
    void opening.catch(() => undefined);
    await harness.listenerRegistered;

    harness.plugin.onunload();

    await expect(opening).rejects.toEqual(expect.objectContaining({ code: "bridge_closed" }));
    expect(harness.offref).toHaveBeenCalledOnce();
    expect(harness.openFile).not.toHaveBeenCalled();
  });

  it("does not register a missing-file wait after unload during managed-vault validation", async () => {
    const nativeSetTimeout = globalThis.setTimeout;
    vi.spyOn(globalThis, "setTimeout").mockImplementation(((callback: () => void, delay?: number) =>
      nativeSetTimeout(callback, delay === 10_000 ? 5 : delay)) as typeof setTimeout);
    const vaultPath = await mkdtemp(path.join(tmpdir(), "open-brain-managed-vault-"));
    const executable = await managedBridgeExecutable(vaultPath);
    let unload = (): void => {};
    const harness = await managedFileHarness(vaultPath, executable, () => queueMicrotask(unload));
    unload = () => harness.plugin.onunload();

    await expect(harness.plugin.openManagedFile("spaces/synthetic/notes/unloaded-missing.md")).rejects.toEqual(
      expect.objectContaining({ code: "bridge_closed" }),
    );
    expect(harness.on).not.toHaveBeenCalled();
    expect(harness.openFile).not.toHaveBeenCalled();
  });

  it("does not open an already registered file after unload during managed-vault validation", async () => {
    const vaultPath = await mkdtemp(path.join(tmpdir(), "open-brain-managed-vault-"));
    const executable = await managedBridgeExecutable(vaultPath);
    const relativePath = "spaces/synthetic/notes/unloaded-existing.md";
    let unload = (): void => {};
    const harness = await managedFileHarness(vaultPath, executable, () => queueMicrotask(unload));
    const file = new harness.TFile(relativePath);
    unload = () => { harness.makeCurrent(file); harness.plugin.onunload(); };

    await expect(harness.plugin.openManagedFile(relativePath)).rejects.toEqual(
      expect.objectContaining({ code: "bridge_closed" }),
    );
    expect(harness.on).not.toHaveBeenCalled();
    expect(harness.openFile).not.toHaveBeenCalled();
  });

  it("rejects a wrong vault before waiting for file registration", async () => {
    const expectedVault = await mkdtemp(path.join(tmpdir(), "open-brain-managed-expected-"));
    const actualVault = await mkdtemp(path.join(tmpdir(), "open-brain-managed-actual-"));
    const executable = await managedBridgeExecutable(expectedVault);
    const harness = await managedFileHarness(actualVault, executable);

    try {
      await expect(harness.plugin.openManagedFile("spaces/synthetic/notes/wrong.md")).rejects.toEqual(
        expect.objectContaining({ code: "wrong_vault" }),
      );
      expect(harness.on).not.toHaveBeenCalled();
    } finally {
      harness.plugin.onunload();
    }
  });

  it("the actual same-vault command approves, matches the page, and opens its confined note", async () => {
    const obsidian = await import("obsidian") as unknown as { __opened: Array<{ contentEl: TestElement; titleEl: TestElement }> };
    obsidian.__opened.length = 0;
    const item = { capture_id: `capture_${uuid(1)}`, payload_family: "text", preview: "Synthetic body",
      space_id: `space_${uuid(500)}`, title: "Synthetic title" };
    const proposalId = `proposal_${uuid(300)}`;
    const pageId = `page_${uuid(400)}`;
    const responses: Record<string, unknown> = {
      "brain.initialize": { status: "initialized" },
      "workspace.status": { status: "ok", vault_path: "/synthetic/managed" },
      "inbox.list": { status: "listed", items: [item], offset: 0, next_offset: null },
      "publication.propose": { status: "proposed", proposal_id: proposalId, page_id: pageId },
      "publication.show": { status: "shown", proposal_status: "pending", proposal_id: proposalId, page_id: pageId,
        space_id: item.space_id, title: item.title, markdown: item.preview, capture_ids: [item.capture_id],
        evidence: [{ capture_id: item.capture_id, excerpt: item.preview, sha256: "a".repeat(64), projection_applied: false }],
        review_token: "d".repeat(64) },
      "publication.approve": { status: "approved", proposal_id: proposalId, page_id: pageId,
        publication_id: `publication_${uuid(900)}` },
      "workspace.refresh": { status: "refreshed", notes: [{ note_id: pageId,
        revision_id: `revision_${uuid(600)}`, relative_path: "Approved.md" }] },
    };
    const bridge = { invoke: vi.fn(async (operation: string) => responses[operation]) };
    const plugin = new OpenBrainPlugin({} as never, {} as never);
    const open = vi.spyOn(plugin, "openManagedFile").mockResolvedValue();
    const running = plugin.reviewCaptures({ bridge: bridge as never, capabilities: publicationCapabilities(), sameVault: async () => true });

    const selection = await openedModal(obsidian.__opened, "Select 1–32 captures for publication");
    const checkbox = walk(selection.contentEl).find((element) => element.tag === "input")!;
    (checkbox as unknown as { checked: boolean }).checked = true;
    checkbox.trigger("change");
    walk(selection.contentEl).find((element) => element.text === "Create draft")!.trigger("click");

    const title = await openedModal(obsidian.__opened, "Publication title");
    walk(title.contentEl).find((element) => element.text === "Continue")!.trigger("click");
    const draft = await openedModal(obsidian.__opened, "Edit publication draft");
    walk(draft.contentEl).find((element) => element.text === "Continue")!.trigger("click");
    const review = await openedModal(obsidian.__opened, "Inspect publication and evidence");
    walk(review.contentEl).find((element) => element.text === "Approve")!.trigger("click");

    await running;
    expect(open).toHaveBeenCalledWith("Approved.md");
    expect(bridge.invoke.mock.calls.map(([operation]) => operation)).toContain("publication.approve");
  });
  it.each(["existing", "create", "cancel"] as const)(
    "the actual unrouted review handles Obsidian close-before-choice order: %s",
    async (choice) => {
      type ChoiceItem = { label: string; value: unknown };
      type ChoiceTestModal = { contentEl: TestElement; titleEl: TestElement; placeholder?: string;
        close(): void; getItems(): ChoiceItem[]; selectSuggestion(item: ChoiceItem): void };
      const obsidian = await import("obsidian") as unknown as {
        __opened: ChoiceTestModal[]; __notices: string[];
      };
      obsidian.__opened.length = 0;
      obsidian.__notices.length = 0;
      const captureId = `capture_${uuid(71)}`;
      const space = { name: "Synthetic space", slug: "synthetic-space", space_id: `space_${uuid(72)}` };
      const bridge = { invoke: vi.fn(async (operation: string, args: Record<string, unknown>) => {
        if (operation === "brain.initialize") return { status: "already_initialized" };
        if (operation === "workspace.status") return { status: "ok", vault_path: "/synthetic/managed" };
        if (operation === "inbox.list") return { status: "listed", offset: 0, next_offset: null,
          items: [{ capture_id: captureId, payload_family: "text", preview: "Synthetic evidence",
            space_id: null, title: null }] };
        if (operation === "space.list") return { status: "listed", spaces: [space], next_offset: null };
        if (operation === "space.create") return { status: "created", space: { ...space, name: args.name } };
        if (operation === "inbox.route") return { status: "routed", capture_id: captureId, space_id: space.space_id };
        throw new Error(`unexpected ${operation}`);
      }) };
      const plugin = new OpenBrainPlugin({} as never, {} as never);
      const running = plugin.reviewCaptures({
        bridge: bridge as never, capabilities: publicationCapabilities(), sameVault: async () => true,
      });
      const selection = await openedModal(obsidian.__opened, "Select 1–32 captures for publication");
      const checkbox = walk(selection.contentEl).find((element) => element.tag === "input")!;
      (checkbox as unknown as { checked: boolean }).checked = true;
      checkbox.trigger("change");
      walk(selection.contentEl).find((element) => element.text === "Create draft")!.trigger("click");
      await vi.waitFor(() => expect(obsidian.__opened.some((modal) =>
        modal.placeholder === "Choose one publication space")).toBe(true));
      const chooser = obsidian.__opened.find((modal) => modal.placeholder === "Choose one publication space")!;
      if (choice === "cancel") {
        chooser.close();
        await running;
        expect(bridge.invoke.mock.calls.map(([operation]) => operation)).not.toContain("inbox.route");
      } else {
        chooser.selectSuggestion(chooser.getItems()[choice === "existing" ? 0 : 1]!);
        if (choice === "create") {
          const name = await openedModal(obsidian.__opened, "New space name");
          walk(name.contentEl).find((element) => element.tag === "input")!.value = "New synthetic space";
          walk(name.contentEl).find((element) => element.text === "Continue")!.trigger("click");
        }
        const title = await openedModal(obsidian.__opened, "Publication title");
        expect(bridge.invoke).toHaveBeenCalledWith("inbox.route", expect.objectContaining({
          capture_id: captureId, space_id: space.space_id,
        }), 30_000);
        (title as unknown as { close(): void }).close();
        await running;
      }
      expect(bridge.invoke.mock.calls.map(([operation]) => operation)).not.toContain("publication.propose");
      expect(obsidian.__notices).toEqual([]);
    },
  );

});

async function managedFileHarness(
  vaultPath: string, executable: string, onGetBasePath: () => void = () => {},
) {
  const obsidian = await import("obsidian") as unknown as {
    FileSystemAdapter: new (basePath: string, onGetBasePath?: () => void) => { getBasePath(): string };
    TFile: new (path: string) => { path: string };
  };
  type File = InstanceType<typeof obsidian.TFile>;
  const listeners = new Map<object, (file: File) => void>();
  let current: File | null = null;
  let signalListenerRegistered!: () => void;
  const listenerRegistered = new Promise<void>((resolve) => {
    signalListenerRegistered = resolve;
  });
  const on = vi.fn((_name: string, callback: (file: File) => void) => {
    const ref = {};
    listeners.set(ref, callback);
    signalListenerRegistered();
    return ref;
  });
  const offref = vi.fn((ref: object) => { listeners.delete(ref); });
  const openFile = vi.fn(async () => undefined);
  const leaf = { openFile, getViewState: () => ({ state: {} }) };
  const app = {
    vault: {
      adapter: new obsidian.FileSystemAdapter(vaultPath, onGetBasePath),
      getAbstractFileByPath: vi.fn(() => current),
      offref,
      on,
    },
    workspace: {
      getLeaf: vi.fn(() => leaf),
      getLeavesOfType: vi.fn(() => []),
      revealLeaf: vi.fn(async () => undefined),
    },
  };
  const plugin = new OpenBrainPlugin(app as never, {} as never);
  plugin.settings = { executablePath: executable, inferencePaused: false };
  return {
    TFile: obsidian.TFile, listenerRegistered, offref, on, openFile, plugin,
    makeCurrent(file: File): void { current = file; },
    register(file: File): void {
      current = file;
      for (const listener of [...listeners.values()]) listener(file);
    },
  };
}

async function managedBridgeExecutable(vaultPath: string): Promise<string> {
  const directory = await mkdtemp(path.join(tmpdir(), "open-brain-managed-bridge-"));
  const executable = path.join(directory, "open-brain");
  const handshake = {
    desktop_only: true, operations: ["graph.review"], product_version: "0.1.0",
    protocol: "open-brain-client", protocol_version: 1, status: "ok",
  };
  const status = {
    active_notes: 1, connected: true, inactive_notes: 0, open_conflicts: 0,
    pending_suggestions: 0, status: "ok", vault_path: vaultPath, workspace_id: `workspace_${uuid(800)}`,
  };
  const body = `#!${process.execPath}
let buffer = "";
const handshake = ${JSON.stringify(handshake)};
const status = ${JSON.stringify(status)};
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let newline = buffer.indexOf("\\n");
  while (newline >= 0) {
    const request = JSON.parse(buffer.slice(0, newline));
    buffer = buffer.slice(newline + 1);
    const result = request.operation === "system.handshake" ? handshake : status;
    process.stdout.write(JSON.stringify({ok:true, protocol:"open-brain-client", protocol_version:1,
      request_id:request.request_id, result}) + "\\n");
    newline = buffer.indexOf("\\n");
  }
});
`;
  await writeFile(executable, body, "utf8");
  await chmod(executable, 0o700);
  return executable;
}

function publicationCapabilities(): Set<string> {
  return new Set(["inbox.list", "inbox.route", "publication.approve", "publication.edit_and_approve",
    "publication.propose", "publication.reject", "publication.show", "space.create", "space.list",
    "workspace.refresh", "workspace.setup", "workspace.status"]);
}

async function openedModal(
  opened: Array<{ contentEl: TestElement; titleEl: TestElement }>,
  title: string,
): Promise<{ contentEl: TestElement; titleEl: TestElement }> {
  let modal: { contentEl: TestElement; titleEl: TestElement } | undefined;
  await vi.waitFor(() => {
    modal = opened.find((candidate) => candidate.titleEl.text === title);
    expect(modal).toBeDefined();
  });
  return modal!;
}
