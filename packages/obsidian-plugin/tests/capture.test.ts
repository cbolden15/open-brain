import { describe, expect, it, vi } from "vitest";

import type { OpenBrainBridge } from "../src/bridge";
import { captureText } from "../src/capture";

const configured = {
  status: "ok", connected: true, active_notes: 1, inactive_notes: 0,
  open_conflicts: 0, pending_suggestions: 0,
  vault_path: "/synthetic/vault", workspace_id: "workspace_synthetic",
};

function bridge(status: unknown = configured) {
  const invoke = vi.fn(async (operation: string) => {
    if (operation === "capture.create") return { status: "captured" };
    if (operation === "workspace.status") return status;
    throw new Error(`Unexpected operation: ${operation}`);
  });
  return { invoke, client: { invoke } as Pick<OpenBrainBridge, "invoke"> };
}

describe("quick capture", () => {
  it("captures and refreshes the graph in this vault without authorizing materialization", async () => {
    const { invoke, client } = bridge();
    const sameVault = vi.fn(async () => true);
    const graph = vi.fn(async () => undefined);
    await captureText(client, "Synthetic capture", sameVault, graph);
    expect(invoke.mock.calls).toEqual([
      ["capture.create", { text: "Synthetic capture" }, 30_000],
      ["workspace.status", {}],
    ]);
    expect(sameVault).toHaveBeenCalledWith("/synthetic/vault");
    expect(graph).toHaveBeenCalledOnce();
  });

  it.each([configured, { status: "unconfigured" }])("does not refresh an unrelated or absent vault", async (status) => {
    const { invoke, client } = bridge(status);
    const graph = vi.fn(async () => undefined);
    await captureText(client, "Synthetic", async () => false, graph);
    expect(invoke.mock.calls.map(([operation]) => operation)).toEqual(["capture.create", "workspace.status"]);
    expect(graph).not.toHaveBeenCalled();
  });

  it("propagates capture failure to the existing notice handler without follow-up operations", async () => {
    const { invoke, client } = bridge();
    invoke.mockRejectedValueOnce(new Error("capture failed"));
    const graph = vi.fn(async () => undefined);
    await expect(captureText(client, "Synthetic", async () => true, graph)).rejects.toThrow("capture failed");
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(graph).not.toHaveBeenCalled();
  });

  it("propagates status and graph errors to the existing notice handler", async () => {
    const invalid = bridge({ status: "invalid" });
    const graph = vi.fn(async () => { throw new Error("graph failed"); });
    await expect(captureText(invalid.client, "Synthetic", async () => true, graph)).rejects.toThrow("invalid workspace status");
    expect(graph).not.toHaveBeenCalled();
    await expect(captureText(bridge().client, "Synthetic", async () => true, graph)).rejects.toThrow("graph failed");
  });
});
