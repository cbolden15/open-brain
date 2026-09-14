// @vitest-environment jsdom
import { invoke } from "@tauri-apps/api/core";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
const mockedInvoke = vi.mocked(invoke);
const status = { status: "ok", brain_root: "/synthetic/brain", initialized: false, state_schema_version: 4, runtime_session_version: 1 };
const preview = {
  status: "preview", preview_id: "setup_synthetic", client: "claude-code", scope: "project",
  action: "configure", brain_root: "/synthetic/brain", runtime_path: "/synthetic/open-brain",
  permissions: { capture: true, search: false },
  changes: [{ path: "/synthetic/project/.mcp.json", kind: "mcp", operation: "add", content: "Only the owned fragment" }], notices: [],
};

beforeEach(() => {
  mockedInvoke.mockReset();
  mockedInvoke.mockResolvedValue(status);
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
    await screen.findByText("Saved to your Brain.");
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
    mockedInvoke.mockImplementation(async (_command, input) =>
      (input as { operation: string }).operation === "system.status" ? status : {
        results: [{ result_id: "synthetic", title: unsafeText, excerpt: "Untrusted source content", record_type: "source", source_origin: "imported", trust: "unverified" }],
      });
    await ready();
    fireEvent.change(screen.getByLabelText("Search your Brain"), { target: { value: "synthetic" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText(unsafeText);
    expect(document.querySelector(".search-result img")).toBeNull();
    expect(screen.getByText("Unverified source")).toBeTruthy();
  });

  it("never displays raw configuration-bearing errors", async () => {
    mockedInvoke.mockRejectedValue('parser error with private_setting="synthetic-secret"');
    render(<App />);
    await waitFor(() => expect(screen.queryByText("Opening runtime…")).toBeNull());
    expect(screen.getByRole("alert").textContent).not.toContain("synthetic-secret");
    expect(screen.getByRole("button", { name: "Retry connection" })).toBeTruthy();
  });
});
