import type { TFile, Workspace, WorkspaceLeaf } from "obsidian";
import { describe, expect, it, vi } from "vitest";

import { openCanvasFile } from "../src/canvas";

function leafFor(path: string): WorkspaceLeaf {
  return {
    getViewState: () => ({ state: { file: path }, type: "canvas" }),
    openFile: vi.fn().mockResolvedValue(undefined),
  } as unknown as WorkspaceLeaf;
}

describe("openCanvasFile", () => {
  it("reopens and reveals an existing leaf for the generated Canvas", async () => {
    const file = { path: "Open Brain Graph.canvas" } as TFile;
    const other = leafFor("Other.canvas");
    const existing = leafFor(file.path);
    const getLeaf = vi.fn();
    const revealLeaf = vi.fn().mockResolvedValue(undefined);
    const workspace = {
      getLeaf,
      getLeavesOfType: vi.fn(() => [other, existing]),
      revealLeaf,
    } as unknown as Workspace;

    await openCanvasFile(workspace, file);

    expect(getLeaf).not.toHaveBeenCalled();
    expect(existing.openFile).toHaveBeenCalledWith(file, { active: true });
    expect(revealLeaf).toHaveBeenCalledWith(existing);
  });

  it("opens one new tab when the generated Canvas is not already open", async () => {
    const file = { path: "Open Brain Graph.canvas" } as TFile;
    const created = leafFor(file.path);
    const getLeaf = vi.fn(() => created);
    const revealLeaf = vi.fn().mockResolvedValue(undefined);
    const workspace = {
      getLeaf,
      getLeavesOfType: vi.fn(() => []),
      revealLeaf,
    } as unknown as Workspace;

    await openCanvasFile(workspace, file);

    expect(getLeaf).toHaveBeenCalledWith("tab");
    expect(created.openFile).toHaveBeenCalledWith(file, { active: true });
    expect(revealLeaf).toHaveBeenCalledWith(created);
  });
});
