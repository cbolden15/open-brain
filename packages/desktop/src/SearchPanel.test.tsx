// @vitest-environment jsdom
import { invoke } from "@tauri-apps/api/core";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SearchPanel } from "./SearchPanel";
import type { RecordSummary } from "./client";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
const mockedInvoke = vi.mocked(invoke);
const operations = new Set(["search.page", "record.read"]);

function hit(index: number, title = `Synthetic ${index}`): RecordSummary {
  const suffix = String(index).padStart(12, "0");
  const recordId = `capture_123e4567-e89b-42d3-a456-${suffix}`;
  return {
    record_id: recordId, revision_id: recordId, source_id: `source_123e4567-e89b-42d3-a456-${suffix}`,
    record_type: "source", payload_family: "text", space_id: null, title, excerpt: `Excerpt ${index}`, trust: "third_party",
    provenance: { representative_capture_id: recordId, capture_ids: [recordId], source_origin: "third_party" },
    source_update_available: false,
  };
}

afterEach(cleanup);
beforeEach(() => mockedInvoke.mockReset());

describe("negotiated desktop search", () => {
  it("traverses at least 201 results once and preserves explicit filters", async () => {
    const pages = [Array.from({ length: 100 }, (_, index) => hit(index)), Array.from({ length: 100 }, (_, index) => hit(index + 100)), [hit(200)]];
    let page = 0;
    mockedInvoke.mockImplementation(async () => ({ status: "ok", dto_version: 1, results: pages[page], next_cursor: page++ < 2 ? `cursor-${page}` : null, complete: page === 3, mode_used: "lexical", warnings: [] }));
    render(<SearchPanel disabled={false} operations={operations} onCapture={() => undefined} />);
    fireEvent.change(screen.getByLabelText("Search your Brain"), { target: { value: "synthetic launch" } });
    fireEvent.click(screen.getByText("Filters and mode"));
    fireEvent.click(screen.getByLabelText("Captures"));
    fireEvent.click(screen.getByLabelText("event"));
    fireEvent.change(screen.getByLabelText("Space IDs"), { target: { value: "space_123e4567-e89b-42d3-a456-426614174500" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("100 matches");
    fireEvent.click(screen.getByRole("button", { name: "Load more results" }));
    await screen.findByText("200 matches");
    fireEvent.click(screen.getByRole("button", { name: "Load more results" }));
    await screen.findByText("201 matches");
    expect(new Set(screen.getAllByRole("article").map(article => article.textContent)).size).toBe(201);
    const first = mockedInvoke.mock.calls[0]?.[1] as { arguments: { filters: object } };
    expect(first.arguments.filters).toEqual({ space_ids: ["space_123e4567-e89b-42d3-a456-426614174500"], payload_families: ["event"], record_types: ["source"] });
  });

  it("surfaces a stale cursor and restarts only after an explicit action", async () => {
    let calls = 0;
    mockedInvoke.mockImplementation(async () => {
      calls += 1;
      if (calls === 2) throw "cursor_stale";
      return { status: "ok", dto_version: 1, results: [hit(calls)], next_cursor: calls === 1 ? "cursor-one" : null, complete: calls !== 1, mode_used: "lexical", warnings: [] };
    });
    render(<SearchPanel disabled={false} operations={operations} onCapture={() => undefined} />);
    fireEvent.change(screen.getByLabelText("Search your Brain"), { target: { value: "stable query" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByRole("button", { name: "Load more results" });
    fireEvent.click(screen.getByRole("button", { name: "Load more results" }));
    await screen.findByRole("button", { name: "Restart this search" });
    expect(calls).toBe(2);
    fireEvent.click(screen.getByRole("button", { name: "Restart this search" }));
    await screen.findByText("Synthetic 3");
    const restart = mockedInvoke.mock.calls[2]?.[1] as { arguments: { query: string; cursor: string | null } };
    expect(restart.arguments).toMatchObject({ query: "stable query", cursor: null });
  });

  it("reconstructs exact Unicode chunks and renders hostile content as inert text", async () => {
    const chunks = ['<script>window.hostile = true</script>\n', "🪴 café 漢字".repeat(3000), " end 🧠".repeat(3000)];
    const unsafe = chunks.join("");
    const summary = hit(9, '<img src="x" onerror="alert(1)">');
    let reads = 0;
    mockedInvoke.mockImplementation(async (_command, input) => {
      if (!input) return undefined;
      const request = input as { operation: string };
      if (request.operation === "search.page") return { status: "ok", dto_version: 1, results: [summary], next_cursor: null, complete: true, mode_used: "hybrid", warnings: ["projection_stale"] };
      const text = chunks[reads] ?? "";
      const start = new TextEncoder().encode(chunks.slice(0, reads).join("")).length;
      reads += 1;
      return { status: "ok", dto_version: 1, record: summary, content: { kind: "untrusted_text", text }, start_byte: start, end_byte: start + new TextEncoder().encode(text).length, next_cursor: reads < chunks.length ? `read-${reads}` : null, complete: reads === chunks.length };
    });
    render(<SearchPanel disabled={false} operations={operations} onCapture={() => undefined} />);
    fireEvent.change(screen.getByLabelText("Search your Brain"), { target: { value: "hostile" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText(summary.title);
    expect(document.querySelector(".search-result img")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Read complete record" }));
    await waitFor(() => expect(screen.getByLabelText("Complete record").querySelector("pre")?.textContent).toBe(unsafe));
    expect(document.querySelector(".record-reader script")).toBeNull();
    expect(reads).toBe(3);
  });

  it("disables absent capabilities with a useful explanation", () => {
    render(<SearchPanel disabled={false} operations={new Set()} onCapture={() => undefined} />);
    expect(screen.getByRole("status").textContent).toContain("unavailable");
    expect((screen.getByRole("button", { name: "Search" }) as HTMLButtonElement).disabled).toBe(true);
    expect(mockedInvoke).not.toHaveBeenCalled();
  });

  it("accepts valid empty display strings and rejects response grammar extensions", async () => {
    mockedInvoke.mockResolvedValueOnce({ status: "ok", dto_version: 1, results: [hit(8, "")], next_cursor: null, complete: true, mode_used: "lexical", warnings: [] });
    render(<SearchPanel disabled={false} operations={operations} onCapture={() => undefined} />);
    fireEvent.change(screen.getByLabelText("Search your Brain"), { target: { value: "empty title" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Excerpt 8");
    cleanup();

    mockedInvoke.mockResolvedValueOnce({ status: "ok", dto_version: 1, results: [{ ...hit(9), unknown: true }], next_cursor: null, complete: true, mode_used: "lexical", warnings: [] });
    render(<SearchPanel disabled={false} operations={operations} onCapture={() => undefined} />);
    fireEvent.change(screen.getByLabelText("Search your Brain"), { target: { value: "malformed result" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByRole("alert");
    expect(screen.queryByText("Synthetic 9")).toBeNull();
  });
});
