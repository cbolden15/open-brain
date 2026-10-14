import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("obsidian", () => {
  const doc = { activeElement: null as unknown };
  class Element {
    public children: Element[] = [];
    public disabled = false;
    public checked = false;
    public isConnected = true;
    public attributes: Record<string, string> = {};
    public style: Record<string, string> = {};
    public events: Record<string, () => void> = {};
    public ownerDocument = doc;
    public constructor(public tag = "div", public text = "") {}
    public addClass(): void {}
    public createEl(tag: string, options: { text?: string; type?: string } = {}): Element {
      const child = new Element(tag, options.text ?? ""); if (options.type) child.setAttribute("type", options.type); this.children.push(child); return child;
    }
    public createSpan(options: { text?: string } = {}): Element { return this.createEl("span", options); }
    public createDiv(): Element { return this.createEl("div"); }
    public setText(text: string): void { this.text = text; }
    public setAttribute(key: string, value: string): void { this.attributes[key] = value; }
    public getAttribute(key: string): string | null { return this.attributes[key] ?? null; }
    public empty(): void { for (const child of this.children) child.isConnected = false; this.children = []; }
    public addEventListener(name: string, callback: () => void): void { this.events[name] = callback; }
    public trigger(name: string): void { this.events[name]?.(); }
    public focus(): void { doc.activeElement = this; }
    // Native buttons activate from Enter/Space; checkbox change is separate.
    public key(key: string): void { if (this.tag === "button" && (key === "Enter" || key === " ")) this.trigger("click"); }
  }
  class Modal {
    public contentEl = new Element(); public titleEl = new Element();
    public constructor(public app: unknown) {}
    public open(): void { (this as { onOpen?: () => void }).onOpen?.(); }
    public close(): void { (this as { onClose?: () => void }).onClose?.(); }
  }
  return { Modal, __Element: Element, __doc: doc };
});

import { FacetClient } from "../src/facet-client";
import { FacetWireError } from "../src/facet-wire";
import { FacetReconciliationModal, openFacetReconciliation } from "../src/facet-reconciliation";
import type { FacetOperation, Wire_facet_vault_preview_response, Wire_facet_vault_apply_response } from "../src/facet-types";

type TestElement = {
  children: TestElement[]; tag: string; text: string; checked: boolean; disabled: boolean;
  attributes: Record<string, string>; isConnected: boolean; focus(): void; key(key: string): void; trigger(name: string): void;
};
const walk = (element: TestElement): TestElement[] => [element, ...element.children.flatMap(walk)];
const elements = (modal: FacetReconciliationModal) => walk(modal.contentEl as unknown as TestElement);
const text = (modal: FacetReconciliationModal) => elements(modal).map((element) => element.text).join("\n");
const button = (modal: FacetReconciliationModal, label: string) => elements(modal).find((element) => element.tag === "button" && element.text === label)!;
const confirm = (modal: FacetReconciliationModal) => {
  const check = elements(modal).find((element) => element.tag === "input")!; check.checked = true; check.trigger("change");
};
const fixture = JSON.parse(readFileSync(new URL("fixtures/facets/o0-cases.json", import.meta.url), "utf8")) as {
  handshake: Record<string, unknown>; frames: { operation: FacetOperation; result: unknown }[];
};
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const response = (operation: string) => copy(fixture.frames.find((entry) => entry.operation === operation)!.result);
const preview = (overrides: Partial<Wire_facet_vault_preview_response> = {}): Wire_facet_vault_preview_response => ({
  ...response("facet.vault.preview") as Wire_facet_vault_preview_response, expires_at: "2030-10-13T12:15:00.000000Z", ...overrides,
});
const receipt = (args: Record<string, unknown>, refresh = false): Wire_facet_vault_apply_response => {
  const result = response("facet.vault.apply") as Wire_facet_vault_apply_response;
  return { ...result, projection_status: refresh ? "refresh_required" : "current", receipt: { ...result.receipt, operation_id: String(args.operation_id) } };
};
const workspaceId = "workspace_00000000-0000-4000-8000-000000000001";
const recordId = "page_00000000-0000-4000-8000-000000000001";
const opened: FacetReconciliationModal[] = [];
type Handler = (operation: string, args: Record<string, unknown>) => Promise<unknown>;
function harness(handler?: Handler, handshake = fixture.handshake) {
  const invoke = vi.fn(async (operation: string, args: Record<string, unknown>) => {
    if (handler) return await handler(operation, args);
    if (operation === "facet.vault.preview") return preview();
    if (operation === "facet.vault.apply") return receipt(args);
    return response(operation);
  });
  const client = new FacetClient({ invoke } as never, copy(handshake));
  const refreshManaged = vi.fn(async () => undefined), editNote = vi.fn(async () => undefined);
  const options = { workspaceId, recordId, refreshManaged, editNote };
  const modal = openFacetReconciliation({} as never, client, options); opened.push(modal);
  return { modal, client, invoke, options, refreshManaged, editNote };
}
const ready = async (modal: FacetReconciliationModal) => { await vi.waitFor(() => expect(text(modal)).toContain("Current classification version")); };
const applyCount = (invoke: ReturnType<typeof vi.fn>) => invoke.mock.calls.filter(([operation]) => operation === "facet.vault.apply").length;
afterEach(() => { for (const modal of opened.splice(0)) modal.dispose(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe("Markdown facet reconciliation UI (synthetic transport, not engine proof)", () => {
  it("previews the engine record identity and explicitly lists all missing-subtree removals without autoapply", async () => {
    const values = preview().add_value_ids;
    const h = harness(async (operation) => operation === "facet.vault.preview" ? preview({ add_value_ids: [], remove_value_ids: values }) : response(operation));
    await ready(h.modal);
    await vi.waitFor(() => expect(text(h.modal)).toContain("Project: Apollo"));
    expect(h.invoke).toHaveBeenCalledWith("facet.vault.preview", { dto_version: 1, workspace_id: workspaceId, record_id: recordId });
    expect(text(h.modal)).toContain("Proposed removals"); expect(text(h.modal)).toContain(values[0]);
    expect(text(h.modal)).toContain("Missing or empty facets means removal of all current memberships");
    expect(text(h.modal)).toContain("2030-10-13T12:15:00.000000Z");
    expect(button(h.modal, "Apply reviewed facets").disabled).toBe(true); expect(applyCount(h.invoke)).toBe(0);
  });

  it.each(["unknown_name", "ambiguous_name", "archived_addition", "content_changed", "projection_changed", "invalid_frontmatter"] as const)(
    "explains %s and blocks Apply even if a disabled confirmation is triggered", async (code) => {
      const h = harness(async (operation) => operation === "facet.vault.preview" ? preview({ conflicts: [{ code, value_id: null }] }) : response(operation));
      await ready(h.modal); confirm(h.modal); button(h.modal, "Apply reviewed facets").trigger("click");
      expect(text(h.modal)).toContain("Conflict:"); expect(button(h.modal, "Apply reviewed facets").disabled).toBe(true);
      expect(applyCount(h.invoke)).toBe(0); expect(button(h.modal, "Edit note").disabled).toBe(false);
    },
  );

  it("renders all conflicts together and falls back to stable IDs if vocabulary reads fail", async () => {
    const h = harness(async (operation) => {
      if (operation === "facet.vault.preview") return preview({ conflicts: [{ code: "content_changed", value_id: null }, { code: "unknown_name", value_id: null }] });
      throw new FacetWireError("not_found");
    });
    await ready(h.modal); await vi.waitFor(() => expect(text(h.modal)).toContain("Vocabulary labels are unavailable"));
    expect(text(h.modal)).toContain("note body changed"); expect(text(h.modal)).toContain("facet label is unknown");
    expect(text(h.modal)).toContain(preview().add_value_ids[0]); expect(applyCount(h.invoke)).toBe(0);
  });

  it("applies only the preview ID and one stable operation ID, with double-click protection", async () => {
    let release!: (result: unknown) => void;
    const h = harness(async (operation, args) => {
      if (operation === "facet.vault.apply") return await new Promise((resolve) => { release = resolve; });
      return operation === "facet.vault.preview" ? preview() : response(operation);
    });
    await ready(h.modal); confirm(h.modal); const apply = button(h.modal, "Apply reviewed facets");
    apply.key("Enter"); apply.trigger("click");
    await vi.waitFor(() => expect(applyCount(h.invoke)).toBe(1));
    const request = h.invoke.mock.calls.find(([operation]) => operation === "facet.vault.apply")![1];
    expect(Object.keys(request).sort()).toEqual(["dto_version", "operation_id", "preview_id"]);
    expect(request.preview_id).toBe("synthetic-preview-1");
    expect(button(h.modal, "Refresh preview").disabled).toBe(true);
    release(receipt(request)); await vi.waitFor(() => expect(text(h.modal)).toContain("Classification committed"));
    expect(text(h.modal)).toContain(String(request.operation_id)); expect(applyCount(h.invoke)).toBe(1);
  });

  it.each(["timeout", "bridge_closed", "operation_pending"])("retains exact request after %s and retries the same operation", async (code) => {
    let count = 0;
    const h = harness(async (operation, args) => {
      if (operation === "facet.vault.apply") { count += 1; if (count === 1) throw new FacetWireError(code); return receipt(args); }
      return operation === "facet.vault.preview" ? preview() : response(operation);
    });
    await ready(h.modal); confirm(h.modal); button(h.modal, "Apply reviewed facets").trigger("click");
    await vi.waitFor(() => expect(text(h.modal)).toContain("result is uncertain"));
    expect(button(h.modal, "Refresh preview").disabled).toBe(true); expect(button(h.modal, "Edit note").disabled).toBe(true);
    button(h.modal, "Retry same operation").key(" "); await vi.waitFor(() => expect(text(h.modal)).toContain("Classification committed"));
    const calls = h.invoke.mock.calls.filter(([operation]) => operation === "facet.vault.apply");
    expect(JSON.stringify(calls[1]![1])).toBe(JSON.stringify(calls[0]![1]));
  });

  it("requires a fresh preview and fresh confirmation after engine stale refusal", async () => {
    let previews = 0, writes = 0;
    const h = harness(async (operation, args) => {
      if (operation === "facet.vault.preview") return preview({ preview_id: `synthetic-preview-${++previews}` });
      if (operation === "facet.vault.apply") { if (++writes === 1) throw new FacetWireError("preview_stale"); return receipt(args); }
      return response(operation);
    });
    await ready(h.modal); confirm(h.modal); button(h.modal, "Apply reviewed facets").trigger("click");
    await vi.waitFor(() => expect(text(h.modal)).toContain("expired or the note or classification changed"));
    expect(button(h.modal, "Apply reviewed facets").disabled).toBe(true);
    button(h.modal, "Refresh preview").trigger("click"); await vi.waitFor(() => expect(previews).toBe(2)); await ready(h.modal);
    expect(button(h.modal, "Apply reviewed facets").disabled).toBe(true); expect(writes).toBe(1);
    confirm(h.modal); button(h.modal, "Apply reviewed facets").trigger("click"); await vi.waitFor(() => expect(writes).toBe(2));
    const calls = h.invoke.mock.calls.filter(([operation]) => operation === "facet.vault.apply");
    expect(calls[1]![1].preview_id).toBe("synthetic-preview-2"); expect(calls[1]![1].operation_id).not.toBe(calls[0]![1].operation_id);
  });

  it("disables Apply when expiry passes and resets confirmation on refreshed previews", async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-13T12:00:00.000Z"));
    const h = harness(async (operation) => operation === "facet.vault.preview" ? preview({ expires_at: "2026-10-13T12:00:01.000000Z" }) : response(operation));
    await vi.advanceTimersByTimeAsync(0); confirm(h.modal); expect(button(h.modal, "Apply reviewed facets").disabled).toBe(false);
    await vi.advanceTimersByTimeAsync(1001); expect(button(h.modal, "Apply reviewed facets").disabled).toBe(true);
    expect(text(h.modal)).toContain("Preview expired"); expect(applyCount(h.invoke)).toBe(0);
  });

  it("preserves committed receipt when refresh fails and retries refresh without another mutation", async () => {
    const h = harness(async (operation, args) => operation === "facet.vault.apply" ? receipt(args, true) : operation === "facet.vault.preview" ? preview() : response(operation));
    h.refreshManaged.mockRejectedValueOnce(new Error("synthetic refresh failure"));
    await ready(h.modal); confirm(h.modal); button(h.modal, "Apply reviewed facets").trigger("click");
    await vi.waitFor(() => expect(text(h.modal)).toContain("managed vault refresh needed"));
    const operationId = h.invoke.mock.calls.find(([operation]) => operation === "facet.vault.apply")![1].operation_id;
    button(h.modal, "Refresh managed vault").trigger("click"); await vi.waitFor(() => expect(text(h.modal)).toContain("vault refresh failed"));
    expect(text(h.modal)).toContain(String(operationId)); expect(text(h.modal)).toContain("generated-index conflicts must be preserved");
    button(h.modal, "Refresh managed vault").trigger("click"); await vi.waitFor(() => expect(text(h.modal)).toContain("managed vault refreshed"));
    expect(h.refreshManaged).toHaveBeenCalledTimes(2); expect(applyCount(h.invoke)).toBe(1);
    expect(elements(h.modal).some((element) => element.text === "Apply reviewed facets")).toBe(false);
  });

  it("opens the note explicitly and requires a refreshed preview before re-confirmation", async () => {
    const h = harness(); await ready(h.modal); confirm(h.modal); button(h.modal, "Edit note").trigger("click");
    await vi.waitFor(() => expect(h.editNote).toHaveBeenCalledOnce()); await vi.waitFor(() => expect(text(h.modal)).toContain("Edit the note, then request a fresh preview"));
    confirm(h.modal); expect(button(h.modal, "Apply reviewed facets").disabled).toBe(true); expect(applyCount(h.invoke)).toBe(0);
  });

  it("cannot use a detached old confirmation to confirm a different preview", async () => {
    const h = harness(); await ready(h.modal); await vi.waitFor(() => expect(text(h.modal)).toContain("Project: Apollo"));
    const oldCheck = elements(h.modal).find((element) => element.tag === "input")!;
    button(h.modal, "Refresh preview").trigger("click"); await ready(h.modal);
    oldCheck.checked = true; oldCheck.trigger("change");
    expect(button(h.modal, "Apply reviewed facets").disabled).toBe(true); expect(applyCount(h.invoke)).toBe(0);
  });

  it("ignores superseded vocabulary reads when the owner requests a new preview", async () => {
    let release!: (value: unknown) => void, dimensionReads = 0;
    const h = harness(async (operation) => {
      if (operation === "facet.vault.preview") return preview();
      if (operation === "facet.dimension.list" && ++dimensionReads === 1) return await new Promise((resolve) => { release = resolve; });
      return response(operation);
    });
    await ready(h.modal); button(h.modal, "Refresh preview").trigger("click"); await ready(h.modal);
    await vi.waitFor(() => expect(text(h.modal)).toContain("Project: Apollo"));
    const callsBefore = h.invoke.mock.calls.length; const displayedBefore = text(h.modal);
    release(response("facet.dimension.list")); await Promise.resolve(); await Promise.resolve();
    expect(text(h.modal)).toBe(displayedBefore); expect(h.invoke.mock.calls).toHaveLength(callsBefore);
  });

  it("loads classification event sequence read-only through the shared client", async () => {
    const h = harness(); await ready(h.modal); button(h.modal, "Load classification history").trigger("click");
    await vi.waitFor(() => expect(text(h.modal)).toContain("Sequence 1"));
    expect(text(h.modal)).toContain("assignment.patch"); expect(text(h.modal)).toContain("History is read-only");
    expect(h.invoke).toHaveBeenCalledWith("facet.history.list", { dto_version: 1, subject: preview().subject, cursor: null, limit: 50, as_of_sequence: null });
    expect(h.invoke.mock.calls.some(([operation]) => /restore|unarchive|reconcile/.test(operation))).toBe(false);
  });

  it("warns before closing an uncertain write and loads authoritative history on reopening", async () => {
    const h = harness(async (operation) => { if (operation === "facet.vault.apply") throw new FacetWireError("timeout"); return operation === "facet.vault.preview" ? preview() : response(operation); });
    await ready(h.modal); confirm(h.modal); button(h.modal, "Apply reviewed facets").trigger("click");
    await vi.waitFor(() => expect(text(h.modal)).toContain("result is uncertain"));
    h.modal.close(); expect(text(h.modal)).toContain("cancellation is not rollback"); expect(button(h.modal, "Close and discard retry state")).toBeDefined();
    button(h.modal, "Close and discard retry state").trigger("click"); expect(elements(h.modal)).toHaveLength(1);
    const next = openFacetReconciliation({} as never, h.client, h.options); opened.push(next); await ready(next);
    await vi.waitFor(() => expect(text(next)).toContain("Loaded 1 classification events"));
    expect(button(next, "Apply reviewed facets").disabled).toBe(true); expect(applyCount(h.invoke)).toBe(1);
  });

  it("ignores late preview responses and stale buttons after dispose, returning focus", async () => {
    const obsidian = await import("obsidian") as unknown as { __Element: new () => TestElement; __doc: { activeElement: TestElement | null } };
    const focus = new obsidian.__Element(); focus.focus(); let release!: (value: unknown) => void;
    const h = harness(async (operation) => operation === "facet.vault.preview" ? await new Promise((resolve) => { release = resolve; }) : response(operation));
    const oldRefresh = button(h.modal, "Refresh preview"); h.modal.dispose(); release(preview());
    await Promise.resolve(); await Promise.resolve(); oldRefresh.trigger("click");
    expect(elements(h.modal)).toHaveLength(1); expect(h.invoke).toHaveBeenCalledTimes(1); expect(obsidian.__doc.activeElement).toBe(focus);
  });

  it("does not update disposed UI or invoke refresh after a pending write commits", async () => {
    let release!: (value: unknown) => void;
    const h = harness(async (operation) => operation === "facet.vault.apply" ? await new Promise((resolve) => { release = resolve; }) : operation === "facet.vault.preview" ? preview() : response(operation));
    await ready(h.modal); confirm(h.modal); button(h.modal, "Apply reviewed facets").trigger("click");
    await vi.waitFor(() => expect(applyCount(h.invoke)).toBe(1)); h.modal.dispose();
    const args = h.invoke.mock.calls.find(([operation]) => operation === "facet.vault.apply")![1]; release(receipt(args, true));
    await Promise.resolve(); await Promise.resolve(); expect(elements(h.modal)).toHaveLength(1); expect(h.refreshManaged).not.toHaveBeenCalled();
  });

  it("keeps focus on native keyboard controls through rerenders and announces state", async () => {
    const obsidian = await import("obsidian") as unknown as { __doc: { activeElement: TestElement | null } };
    const h = harness(); await ready(h.modal); const check = elements(h.modal).find((element) => element.tag === "input")!;
    check.focus(); confirm(h.modal); expect(obsidian.__doc.activeElement?.attributes["data-facet-action"]).toBe("confirm");
    expect(elements(h.modal).some((element) => element.attributes.role === "status" && element.attributes["aria-live"] === "polite")).toBe(true);
    expect(button(h.modal, "Apply reviewed facets").attributes.type).toBe("button");
  });

  it("disables only facet UI against older runtimes and never dispatches an unsupported operation", async () => {
    const h = harness(undefined, { protocol: "open-brain-client", protocol_version: 1, status: "ok", operations: [], desktop_only: true, product_version: "0.1.0" });
    expect(text(h.modal)).toContain("runtime lacks Markdown facet reconciliation");
    expect(h.invoke).not.toHaveBeenCalled(); expect(button(h.modal, "Apply reviewed facets").disabled).toBe(true);
  });
});
