// @vitest-environment jsdom
import { invoke } from "@tauri-apps/api/core";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
const mockedInvoke = vi.mocked(invoke);
const status = { status: "ok", brain_root: "/synthetic/brain", initialized: false, state_schema_version: 10, runtime_session_version: 5 };
const contract = {
  status: "ok", contract_version: "t03.v1",
  operations: ["search.page", "record.read"].map(name => ({ name, dto_version: 1, required_grants: [name === "record.read" ? "content-read" : "search"] })),
  limits: { request_bytes: 65536, response_bytes: 1048576, content_calls: 500, content_bytes: 16777216, history_calls: 500, history_bytes: 16777216 },
};
const preview = {
  status: "preview", preview_id: "setup_synthetic", client: "claude-code", scope: "project",
  action: "configure", brain_root: "/synthetic/brain", runtime_path: "/synthetic/open-brain",
  permissions: { capture: true, search: false },
  changes: [{ path: "/synthetic/project/.mcp.json", kind: "mcp", operation: "add", content: "Only the owned fragment" }], notices: [],
};

beforeEach(() => {
  mockedInvoke.mockReset();
  mockedInvoke.mockImplementation(async (_command, input) => (input as { operation?: string }).operation === "contract.describe" ? contract : status);
});
afterEach(cleanup);

async function ready() {
  render(<App />);
  await screen.findByText("Local runtime ready");
}

describe("desktop user operations", () => {
  it("rejects a note over the UTF-8 limit before sending a capture", async () => {
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Capture" }));
    fireEvent.change(screen.getByLabelText("What would you like to remember?"), { target: { value: "🪴".repeat(5000) } });
    expect((screen.getByRole("button", { name: "Save to Brain" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("alert").textContent).toContain("Shorten it");
    expect(mockedInvoke.mock.calls.some(([, input]) => (input as { operation: string }).operation === "capture.create")).toBe(false);
  });

  it("keeps the capture request ID and draft after a lost response", async () => {
    let calls = 0;
    mockedInvoke.mockImplementation(async (_command, input) => {
      const request = input as { operation: string };
      if (request.operation === "system.status") return status;
      if (request.operation === "capture.create") {
        if (++calls === 1) throw "lost_response";
        return { status: "captured" };
      }
      throw "unexpected_operation";
    });
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Capture" }));
    const editor = screen.getByLabelText("What would you like to remember?") as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: "synthetic retry memory" } });
    fireEvent.click(screen.getByRole("button", { name: "Save to Brain" }));
    await screen.findByRole("alert");
    expect(editor.readOnly).toBe(true);
    expect(editor.value).toBe("synthetic retry memory");
    fireEvent.click(screen.getByRole("button", { name: "Retry this save" }));
    await screen.findByText("Captured to your inbox.");
    const captures = mockedInvoke.mock.calls.filter(([, input]) => (input as { operation: string }).operation === "capture.create");
    expect(captures).toHaveLength(2);
    expect(captures[0]?.[1]).toEqual(captures[1]?.[1]);
    expect(editor.value).toBe("");
  });

  it("requires a selected permission and invalidates preview when permissions change", async () => {
    mockedInvoke.mockImplementation(async (_command, input) =>
      (input as { operation: string }).operation === "system.status" ? status : preview);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.change(screen.getByLabelText("Project directory"), { target: { value: "/synthetic/project" } });
    expect((screen.getByRole("button", { name: "Preview setup" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByLabelText(/Allow memory saves/));
    fireEvent.click(screen.getByRole("button", { name: "Preview setup" }));
    await screen.findByText("Nothing changed yet");
    const call = mockedInvoke.mock.calls.find(([, input]) => (input as { operation: string }).operation === "agent.setup.preview");
    expect((call?.[1] as { arguments: object }).arguments).toMatchObject({ allow_capture: true, allow_search: false, project_dir: "/synthetic/project" });
    fireEvent.click(screen.getByLabelText(/Allow Brain search/));
    expect(screen.queryByRole("button", { name: "Apply setup" })).toBeNull();
    expect(mockedInvoke.mock.calls.some(([, input]) => (input as { operation: string }).operation === "agent.setup.apply")).toBe(false);
  });

  it("shows source text as inert text in search results", async () => {
    const unsafeText = '<img src="x" onerror="alert(1)">';
    mockedInvoke.mockImplementation(async (_command, input) => {
      const operation = (input as { operation: string }).operation;
      if (operation === "system.status") return status;
      if (operation === "contract.describe") return contract;
      return {
        status: "ok", dto_version: 1, next_cursor: null, complete: true, mode_used: "lexical", warnings: [],
        results: [{ record_id: "capture_123e4567-e89b-42d3-a456-426614174100", revision_id: "capture_123e4567-e89b-42d3-a456-426614174100", source_id: "source_123e4567-e89b-42d3-a456-426614174200", title: unsafeText, excerpt: "Untrusted source content", record_type: "source", payload_family: "text", space_id: null, trust: "unverified", provenance: { representative_capture_id: "capture_123e4567-e89b-42d3-a456-426614174100", capture_ids: ["capture_123e4567-e89b-42d3-a456-426614174100"], source_origin: "third_party" }, source_update_available: false }],
      };
    });
    await ready();
    fireEvent.change(screen.getByLabelText("Search your Brain"), { target: { value: "synthetic" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText(unsafeText);
    expect(document.querySelector(".search-result img")).toBeNull();
    expect(screen.getByText("Unverified source")).toBeTruthy();
  });

  it("uses collector status and control operations from the desktop", async () => {
    const enabled = {
      status: "ready",
      brain_root: "/synthetic/brain",
      sources: [{
        source_id: "github.fixture.closed",
        status: "enabled",
        outcome: "completed",
        interval_seconds: 900,
        next_run_epoch: 200,
        last_run_epoch: 100,
        last_success_epoch: 100,
        pause_ack_epoch: null,
        captured_count: 1,
        failed_count: 0,
      }],
    };
    const paused = {
      ...enabled,
      sources: [{ ...enabled.sources[0], status: "paused", pause_ack_epoch: 300 }],
    };
    mockedInvoke.mockImplementation(async (_command, input) => {
      const request = input as { operation: string };
      if (request.operation === "system.status") return status;
      if (request.operation === "collector.status") {
        return mockedInvoke.mock.calls.some(([, callInput]) => (callInput as { operation: string }).operation === "collector.pause")
          ? paused
          : enabled;
      }
      if (request.operation === "collector.pause") return paused.sources[0];
      throw "unexpected_operation";
    });
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Sources" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
    await screen.findByText("github.fixture.closed");
    expect(screen.getByRole("button", { name: "Sync now" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    await screen.findByText(/paused/);
    const operations = mockedInvoke.mock.calls.map(([, input]) => (input as { operation: string }).operation);
    expect(operations).toContain("collector.status");
    expect(operations).toContain("collector.pause");
  });

  it("enables a collector source and updates its schedule from the desktop", async () => {
    const source = {
      source_id: "github.fixture/repo#issues?label=goal&owner=cbolden15",
      status: "enabled",
      outcome: "skipped",
      interval_seconds: 900,
      next_run_epoch: 300,
      last_run_epoch: null,
      last_success_epoch: null,
      pause_ack_epoch: null,
      captured_count: 0,
      failed_count: 0,
    };
    mockedInvoke.mockImplementation(async (_command, input) => {
      const request = input as { operation: string; arguments: Record<string, unknown> };
      if (request.operation === "system.status") return status;
      if (request.operation === "collector.enable") return source;
      if (request.operation === "collector.status") return { status: "ready", brain_root: "/synthetic/brain", sources: [source] };
      if (request.operation === "collector.schedule") return { ...source, interval_seconds: request.arguments.interval_seconds };
      throw "unexpected_operation";
    });
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Sources" }));
    fireEvent.change(screen.getByLabelText("Source ID"), { target: { value: source.source_id } });
    fireEvent.click(screen.getByRole("button", { name: "Enable source" }));
    await screen.findByText(source.source_id);
    const schedule = screen.getAllByLabelText(/Every/).at(-1) as HTMLInputElement;
    fireEvent.change(schedule, { target: { value: "120" } });
    fireEvent.blur(schedule);

    await waitFor(() => expect(mockedInvoke.mock.calls.some(([, input]) =>
      (input as { operation: string }).operation === "collector.schedule")).toBe(true));
    const enableCall = mockedInvoke.mock.calls.find(([, input]) => (input as { operation: string }).operation === "collector.enable");
    expect((enableCall?.[1] as { arguments: object }).arguments).toMatchObject({
      credential_ref: "github-user-token:fixture",
      source_id: source.source_id,
      interval_seconds: 900,
    });
  });

  it("never displays raw configuration-bearing errors", async () => {
    mockedInvoke.mockRejectedValue('parser error with private_setting="synthetic-secret"');
    render(<App />);
    await waitFor(() => expect(screen.queryByText("Opening runtime…")).toBeNull());
    expect(screen.getByRole("alert").textContent).not.toContain("synthetic-secret");
    expect(screen.getByRole("button", { name: "Retry connection" })).toBeTruthy();
  });
});
