import { describe, expect, it } from "vitest";

import { proofSummary, type ProofState } from "./contracts";

describe("proofSummary", () => {
  it("keeps failure codes actionable", () => {
    expect(proofSummary({ kind: "failed", code: "runtime_digest_mismatch" })).toContain(
      "runtime_digest_mismatch",
    );
  });

  it("does not claim cleanup unless the native host confirmed it", () => {
    const state: ProofState = {
      kind: "passed",
      result: {
        captureStatus: "captured",
        cleanupConfirmed: false,
        coreSha256: "a".repeat(64),
        graphifyStatus: "ok",
        graphifySha256: "b".repeat(64),
        matchedSearchResults: 1,
        productVersion: "0.1.0",
        protocol: "open-brain-client",
        protocolVersion: 1,
        target: "aarch64-apple-darwin",
      },
    };
    expect(proofSummary(state)).toContain("not confirmed");
  });
});
