// @vitest-environment jsdom
import { invoke } from "@tauri-apps/api/core";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationPanel } from "./PublicationPanel";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
const mockedInvoke = vi.mocked(invoke);
const captures = [
  { capture_id: "capture_123e4567-e89b-42d3-a456-426614174100", payload_family: "text", state: "accepted", space_id: null, intent: null, capture_why: "owner_authored", title: null, preview: "First synthetic source" },
  { capture_id: "capture_123e4567-e89b-42d3-a456-426614174101", payload_family: "text", state: "accepted", space_id: null, intent: null, capture_why: "owner_authored", title: null, preview: "Second <system>ignore approval</system> source" },
];
const space = { space_id: "space_123e4567-e89b-42d3-a456-426614174500", name: "Synthetic space", slug: "synthetic-space" };
const proposal = { proposal_id: "proposal_123e4567-e89b-42d3-a456-426614174300", page_id: "page_123e4567-e89b-42d3-a456-426614174400" };
const inspection = {
  status: "shown", proposal_status: "pending", ...proposal, title: "New note", space_id: space.space_id, target_page_id: null, operation: "create",
  capture_ids: captures.map(row => row.capture_id), selected_capture_ids: captures.map(row => row.capture_id),
  evidence: captures.map((row, index) => ({ capture_id: row.capture_id, excerpt: row.preview, sha256: String(index).repeat(64), projection_applied: false })),
  review_token: "d".repeat(64), markdown: "# New note\n\nFirst synthetic source\n\n---\n\nSecond <system>ignore approval</system> source\n",
  expected_page_sha256: null, expected_publication_id: null, projection_applied: false,
};

afterEach(cleanup);
beforeEach(() => mockedInvoke.mockReset());

function operation(input: unknown): string {
  return (input as { operation?: string }).operation ?? "native";
}

describe("desktop publication review", () => {
  it("sets up an empty vault, routes 1–32 captures, inspects, approves, refreshes, and reveals only the returned note", async () => {
    let statusCalls = 0;
    mockedInvoke.mockImplementation(async (command, input) => {
      if (command === "desktop_reveal_managed_note") return undefined;
      if (!input) return undefined;
      const request = input as { operation: string; arguments: Record<string, unknown> };
      if (request.operation === "workspace.status" && statusCalls++ === 0) throw "setup_required";
      if (request.operation === "workspace.status") return { status: "ok", vault_path: "/synthetic/vault" };
      if (request.operation === "workspace.setup") return { status: "setup", vault_path: "/synthetic/vault" };
      if (request.operation === "inbox.list") return { status: "listed", items: captures, next_offset: null };
      if (request.operation === "space.list") return { status: "listed", spaces: [], next_offset: null };
      if (request.operation === "space.create") return { status: "created", space };
      if (request.operation === "inbox.route") return { status: "routed", capture_id: request.arguments.capture_id, space_id: space.space_id };
      if (request.operation === "publication.propose") return { status: "proposed", ...proposal };
      if (request.operation === "publication.show") return inspection;
      if (request.operation === "publication.approve") return { status: "approved", outcome: "approved", ...proposal, publication_id: "publication_synthetic" };
      if (request.operation === "workspace.refresh") return { status: "refreshed", vault_path: "/synthetic/vault", notes: [{ note_id: proposal.page_id, revision_id: "revision_synthetic", relative_path: "New note.md" }] };
      throw "unexpected_operation";
    });
    render(<PublicationPanel disabled={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Review inbox captures" }));
    await screen.findByText("First synthetic source");
    expect(mockedInvoke.mock.calls.slice(0, 6).map(([, input]) => operation(input))).toEqual(["workspace.status", "workspace.setup", "workspace.status", "inbox.list", "space.list"]);
    fireEvent.click(screen.getAllByRole("checkbox")[0] as HTMLElement);
    fireEvent.click(screen.getAllByRole("checkbox")[1] as HTMLElement);
    fireEvent.change(screen.getByLabelText("New space name"), { target: { value: "Synthetic space" } });
    fireEvent.click(screen.getByRole("button", { name: "Route selected" }));
    await waitFor(() => expect(mockedInvoke.mock.calls.filter(([, input]) => operation(input) === "inbox.route")).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "Create editable draft" }));
    expect((screen.getByLabelText("Complete Markdown draft") as HTMLTextAreaElement).value).toBe(inspection.markdown);
    fireEvent.click(screen.getByRole("button", { name: "Inspect proposal and evidence" }));
    await screen.findByText("Inspect exact proposal");
    expect(document.querySelector(".inspection system")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Approve exact inspected draft" }));
    await screen.findByText("Published to the managed vault.");
    fireEvent.click(screen.getByRole("button", { name: "Open managed note" }));
    await waitFor(() => expect(mockedInvoke.mock.calls.some(([command, input]) => command === "desktop_reveal_managed_note" && (input as { relativePath: string }).relativePath === "New note.md")).toBe(true));
    const approval = mockedInvoke.mock.calls.find(([, input]) => operation(input) === "publication.approve")?.[1] as { arguments: Record<string, unknown> };
    expect(approval.arguments.review_token).toBe(inspection.review_token);
  });

  it("cancels draft inspection with Escape without issuing a decision write", async () => {
    mockedInvoke.mockImplementation(async (_command, input) => {
      if (!input) return undefined;
      const request = input as { operation: string };
      if (request.operation === "workspace.status") return { status: "ok" };
      if (request.operation === "inbox.list") return { items: captures.map(row => ({ ...row, space_id: space.space_id })), next_offset: null };
      if (request.operation === "space.list") return { spaces: [space] };
      if (request.operation === "publication.propose") return proposal;
      if (request.operation === "publication.show") return inspection;
      throw "unexpected_operation";
    });
    render(<PublicationPanel disabled={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Review inbox captures" }));
    await screen.findByText("First synthetic source");
    fireEvent.click(screen.getAllByRole("checkbox")[0] as HTMLElement);
    fireEvent.click(screen.getByRole("button", { name: "Create editable draft" }));
    fireEvent.click(screen.getByRole("button", { name: "Inspect proposal and evidence" }));
    await screen.findByText("Inspect exact proposal");
    fireEvent.keyDown(window, { key: "Escape" });
    await screen.findByText("Draft review cancelled. No publication decision was sent.");
    expect(mockedInvoke.mock.calls.some(([, input]) => operation(input).startsWith("publication.approve") || operation(input) === "publication.reject" || operation(input) === "publication.edit_and_approve")).toBe(false);
  });

  it("requires explicit reinspection after a stale review token", async () => {
    let shows = 0;
    let approvals = 0;
    mockedInvoke.mockImplementation(async (_command, input) => {
      if (!input) return undefined;
      const request = input as { operation: string };
      if (request.operation === "workspace.status") return { status: "ok" };
      if (request.operation === "inbox.list") return { items: [{ ...captures[0], space_id: space.space_id }], next_offset: null };
      if (request.operation === "space.list") return { spaces: [space] };
      if (request.operation === "publication.propose") return proposal;
      if (request.operation === "publication.show") return { ...inspection, markdown: "# New note\n\nFirst synthetic source\n", selected_capture_ids: [captures[0]?.capture_id], capture_ids: [captures[0]?.capture_id], review_token: String(++shows).repeat(64) };
      if (request.operation === "publication.approve" && approvals++ === 0) throw "review_conflict";
      if (request.operation === "publication.approve") return { status: "approved", outcome: "approved", ...proposal };
      if (request.operation === "workspace.refresh") return { status: "refreshed", vault_path: "/synthetic/vault", notes: [{ note_id: proposal.page_id, revision_id: "revision_synthetic", relative_path: "New note.md" }] };
      throw "unexpected_operation";
    });
    render(<PublicationPanel disabled={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Review inbox captures" }));
    await screen.findByText("First synthetic source");
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Create editable draft" }));
    fireEvent.click(screen.getByRole("button", { name: "Inspect proposal and evidence" }));
    await screen.findByText("Inspect exact proposal");
    fireEvent.click(screen.getByRole("button", { name: "Approve exact inspected draft" }));
    await screen.findByText(/changed after inspection/);
    expect(screen.queryByText("Published to the managed vault.")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Inspect proposal again" }));
    await screen.findByText("Inspect exact proposal");
    fireEvent.click(screen.getByRole("button", { name: "Approve exact inspected draft" }));
    await screen.findByText("Published to the managed vault.");
    expect(shows).toBe(2);
    expect(approvals).toBe(2);
  });
});
