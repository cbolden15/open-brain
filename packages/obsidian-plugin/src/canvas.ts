import type { TFile, Workspace } from "obsidian";

const CANVAS_VIEW_TYPE = "canvas";

export async function openCanvasFile(workspace: Workspace, file: TFile): Promise<void> {
  const leaf =
    workspace
      .getLeavesOfType(CANVAS_VIEW_TYPE)
      .find((candidate) => candidate.getViewState().state?.file === file.path) ??
    workspace.getLeaf("tab");
  await leaf.openFile(file, { active: true });
  await workspace.revealLeaf(leaf);
}
