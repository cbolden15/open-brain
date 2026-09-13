import { describe, expect, it } from "vitest";

import {
  OPEN_BRAIN_CLIENT_PROTOCOL,
  OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
  parseConflictReview,
  parseConflicts,
  parseExclusions,
  parseHandshake,
  parseProviderStatus,
  parseReview,
  parseSemanticRefresh,
  safeRelativePath,
} from "../src/contracts";

describe("plugin result validation", () => {
  it("requires the supported Open Brain client protocol version", () => {
    const handshake = {
      desktop_only: true,
      operations: ["system.handshake"],
      product_version: "0.1.0",
      protocol: OPEN_BRAIN_CLIENT_PROTOCOL,
      protocol_version: OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
      status: "ok",
    } as const;

    expect(parseHandshake(handshake)).toEqual(handshake);
    expect(() => parseHandshake({ ...handshake, protocol_version: 2 })).toThrow(
      "invalid handshake",
    );
    const { protocol_version: _protocolVersion, ...missingVersion } = handshake;
    expect(() => parseHandshake(missingVersion)).toThrow("invalid handshake");
  });

  it("accepts only contained vault-relative source paths", () => {
    expect(safeRelativePath("Notes/First.md")).toBe(true);
    expect(safeRelativePath("../First.md")).toBe(false);
    expect(safeRelativePath("/private/First.md")).toBe(false);
    expect(safeRelativePath("Notes\\First.md")).toBe(false);
  });

  it("rejects unchecked review endpoints", () => {
    expect(() =>
      parseReview({
        append_text: "[[page_00000000-0000-4000-8000-000000000001]]",
        model: "model",
        provider: "openai_api",
        revision_status: "current",
        source: {
          note_id: "page_00000000-0000-4000-8000-000000000002",
          quote: "evidence",
          relative_path: "../escape.md",
          revision_id: "revision_00000000-0000-4000-8000-000000000003",
        },
        status: "review",
        suggestion_id: "suggestion_00000000-0000-4000-8000-000000000004",
        target: {
          note_id: "page_00000000-0000-4000-8000-000000000005",
          quote: "evidence",
          relative_path: "Target.md",
          revision_id: "revision_00000000-0000-4000-8000-000000000006",
        },
      }),
    ).toThrow("invalid review endpoint");
  });

  it("accepts bounded provider status and rejects secret-shaped extra state", () => {
    const status = {
      os_store: "macos_keychain",
      providers: [
        ["openai_api", "api_key", "available", true],
        ["anthropic_api", "api_key", "available", false],
        ["gemini_api", "api_key", "available", false],
        ["claude_subscription", "subscription", "blocked", null],
      ].map(([provider, access_mode, state, credential_saved]) => ({
        access_mode,
        credential_saved,
        provider,
        ...(state === "blocked" ? { reason: "subscription_isolation_unproven" } : {}),
        status: state,
      })),
      selected: null,
      status: "ok",
    };

    expect(parseProviderStatus(status).providers).toHaveLength(4);
    expect(() =>
      parseProviderStatus({
        ...status,
        selected: { api_key: "must-not-appear" },
      }),
    ).toThrow("invalid provider selection");
  });

  it("distinguishes bounded semantic success and failure", () => {
    expect(
      parseSemanticRefresh({
        actual_model: "gpt-6-astra",
        remaining_attempts: 39,
        remaining_input_bytes: 1000,
        status: "refreshed",
      }).status,
    ).toBe("refreshed");
    expect(() =>
      parseSemanticRefresh({
        remaining_attempts: 39,
        remaining_input_bytes: 1000,
        status: "failed",
      }),
    ).toThrow("invalid semantic refresh");
  });

  it("accepts bounded conflict summaries and both complete review versions", () => {
    const conflict = {
      conflict_id: "conflict_00000000-0000-4000-8000-000000000001",
      note_id: "page_00000000-0000-4000-8000-000000000002",
      relative_path: "Notes/Changed.md",
    };
    expect(
      parseConflicts({
        conflicts: [conflict],
        open_conflicts: 1,
        status: "ok",
        workspace_id: "workspace_00000000-0000-4000-8000-000000000003",
      }).conflicts,
    ).toEqual([conflict]);
    expect(
      parseConflictReview({
        accepted_body: "# Accepted\n",
        accepted_revision_id: "revision_00000000-0000-4000-8000-000000000004",
        ...conflict,
        status: "review",
        workspace_body: "# Vault edit\n",
      }).workspace_body,
    ).toBe("# Vault edit\n");
    expect(() =>
      parseConflictReview({
        accepted_body: "# Accepted\n",
        accepted_revision_id: "revision_00000000-0000-4000-8000-000000000004",
        ...conflict,
        relative_path: "../escape.md",
        status: "review",
        workspace_body: "# Vault edit\n",
      }),
    ).toThrow("invalid conflict review");
  });

  it("accepts only contained note and folder exclusions", () => {
    expect(
      parseExclusions({
        exclusions: [
          { kind: "note", relative_path: "Notes/Private.md" },
          { kind: "folder", relative_path: "Private" },
        ],
        status: "ok",
        workspace_id: "workspace_00000000-0000-4000-8000-000000000001",
      }).exclusions,
    ).toHaveLength(2);
    expect(() =>
      parseExclusions({
        exclusions: [{ kind: "folder", relative_path: "../private" }],
        status: "ok",
        workspace_id: "workspace_00000000-0000-4000-8000-000000000001",
      }),
    ).toThrow("invalid exclusion");
  });
});
