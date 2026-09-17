import type { OpenBrainBridge } from "./bridge";
import { parseWorkspaceStatus } from "./contracts";

/** Capture is not authorization to materialize existing notes. */
export async function captureText(
  bridge: Pick<OpenBrainBridge, "invoke">,
  text: string,
  sameVault: (path: string) => Promise<boolean>,
  refreshGraph: () => Promise<unknown>,
): Promise<void> {
  await bridge.invoke("capture.create", { text }, 30_000);
  const status = parseWorkspaceStatus(await bridge.invoke("workspace.status", {}));
  if (status !== null && (await sameVault(status.vault_path))) {
    await refreshGraph();
  }
}
