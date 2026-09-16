// @vitest-environment jsdom
import { invoke } from "@tauri-apps/api/core";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SourceCapturePanel } from "./SourceCapturePanel";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
const mockedInvoke = vi.mocked(invoke);
const emptyStatus = { schema_version: 1, sources: [], collector: { background: false, connected: true } };

beforeEach(() => { mockedInvoke.mockReset(); mockedInvoke.mockResolvedValue(emptyStatus); });
afterEach(cleanup);

function operation(input: unknown): string { return (input as { operation: string }).operation; }

describe("SourceCapturePanel", () => {
  it("discloses account-wide provider scopes while selection remains bounded", async () => {
    render(<SourceCapturePanel />);
    await screen.findByText("Collector connected");
    expect(screen.getByText(/reads all mail in this Google account/)).toBeTruthy();
    expect(screen.getByText(/reads all files in this Google account/)).toBeTruthy();
    expect(screen.getByText(/reads public and private channels you can access/)).toBeTruthy();
    expect(screen.getByText(/Summaries and transcripts both start off/)).toBeTruthy();
    expect((screen.getByLabelText(/Capture extractive session summaries/) as HTMLInputElement).checked).toBe(false);
    expect((screen.getByLabelText(/Capture permitted transcript text/) as HTMLInputElement).checked).toBe(false);
  });

  it("saves a Gmail selection before previewing and imports only the returned preview", async () => {
    const source = { source_id: "source-gmail", selection: { connector_name: "gmail", connection_id: "account-1", resource_id: "label-inbox", resource_type: "label" }, options: {}, status: "disabled", interval_seconds: 900, next_run_epoch: null, pause_ack_epoch: null, last_run: null };
    mockedInvoke.mockImplementation(async (_command, input) => {
      switch (operation(input)) {
        case "sources.status": return emptyStatus;
        case "sources.accounts": return { accounts: [{ provider: "gmail", connection_id: "account-1", display_name: "Synthetic Gmail", scopes: [], expires_at_epoch: null }] };
        case "sources.resources": return { resources: [{ resource_id: "label-inbox", name: "Inbox", resource_type: "label" }], next_cursor: null };
        case "sources.configure": return source;
        case "sources.preview": return { preview_id: "preview-1", source_id: source.source_id, count: 1, has_more: false, notices: [], records: [{ title: '<img src="x">', source_reference: "gmail:synthetic", trust: "unverified" }] };
        case "sources.import": return { source_id: source.source_id, outcome: "completed", captured_count: 1, duplicate_count: 0, has_more: false, notices: [] };
        default: throw "unexpected_operation";
      }
    });
    render(<SourceCapturePanel />);
    await screen.findByText("Collector connected");
    fireEvent.click(screen.getAllByRole("button", { name: "Refresh accounts" })[0]!);
    await screen.findByRole("option", { name: "Synthetic Gmail" });
    fireEvent.change(screen.getAllByLabelText("Connected account", { selector: "select" })[0]!, { target: { value: "account-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Load labels" }));
    await screen.findByRole("option", { name: "Inbox" });
    fireEvent.change(screen.getByLabelText("One label to import"), { target: { value: "label-inbox" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Save selection" })[0]!);
    await screen.findByRole("button", { name: "Preview import" });
    fireEvent.click(screen.getByRole("button", { name: "Preview import" }));
    await screen.findByText('<img src="x">');
    expect(document.querySelector(".source-preview img")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Import preview" }));
    await screen.findByText(/Import completed/);
    const calls = mockedInvoke.mock.calls.map(([, input]) => operation(input));
    expect(calls.indexOf("sources.configure")).toBeLessThan(calls.indexOf("sources.preview"));
    expect(calls.indexOf("sources.preview")).toBeLessThan(calls.indexOf("sources.import"));
    const imported = mockedInvoke.mock.calls.find(([, input]) => operation(input) === "sources.import");
    expect((imported?.[1] as { arguments: object }).arguments).toEqual({ source_id: "source-gmail", preview_id: "preview-1" });
  });

  it("requires an absolute project path and previews session hooks before applying", async () => {
    mockedInvoke.mockImplementation(async (_command, input) => {
      if (operation(input) === "sources.status") return emptyStatus;
      if (operation(input) === "sources.session_preview") return { preview_id: "session-preview", client: "claude_code", project_path: "/synthetic/project", changes: ["Add owned hook"], capture_summary: false, capture_transcript: false, action: "configure" };
      if (operation(input) === "sources.session_apply") return { status: "configured", source_id: "session-source" };
      throw "unexpected_operation";
    });
    render(<SourceCapturePanel />);
    await screen.findByText("Collector connected");
    fireEvent.click(screen.getByRole("button", { name: "Preview session hooks" }));
    expect(screen.getByRole("alert").textContent).toContain("absolute path");
    fireEvent.change(screen.getByLabelText("Project directory"), { target: { value: "/synthetic/project" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview session hooks" }));
    await screen.findByText("Nothing has changed yet.");
    expect(mockedInvoke.mock.calls.some(([, input]) => operation(input) === "sources.session_apply")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Apply hooks" }));
    await screen.findByText(/Automatic capture remains disabled/);
  });

  it("does not issue requests when disabled", async () => {
    render(<SourceCapturePanel disabled />);
    await waitFor(() => expect(mockedInvoke).not.toHaveBeenCalled());
    expect((screen.getByRole("button", { name: "Refresh source status" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
