export interface NativeProofResult {
  captureStatus: string;
  cleanupConfirmed: boolean;
  coreSha256: string;
  graphifyStatus: string;
  graphifySha256: string;
  matchedSearchResults: number;
  productVersion: string;
  protocol: string;
  protocolVersion: number;
  target: string;
}

export type ProofState =
  | { kind: "running" }
  | { kind: "passed"; result: NativeProofResult }
  | { kind: "failed"; code: string };

export function proofSummary(state: ProofState): string {
  if (state.kind === "running") return "Running isolated native proof…";
  if (state.kind === "failed") return `Native proof failed: ${state.code}`;
  return state.result.cleanupConfirmed
    ? "Native capture, search, Graphify, and process cleanup passed."
    : "Native operations passed, but cleanup was not confirmed.";
}
