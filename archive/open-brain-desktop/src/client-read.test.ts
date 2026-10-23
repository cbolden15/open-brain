import { invoke } from "@tauri-apps/api/core";
import { beforeEach, expect, it, vi } from "vitest";
import { readCompleteRecord, type RecordSummary } from "./client";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
const mockedInvoke = vi.mocked(invoke);
const id = "capture_123e4567-e89b-42d3-a456-426614174100";
const record: RecordSummary = {
  record_id: id, revision_id: id, source_id: "source_123e4567-e89b-42d3-a456-426614174200",
  record_type: "source", payload_family: "text", space_id: null, title: "Synthetic", excerpt: "",
  trust: "third_party", source_update_available: false,
  provenance: { representative_capture_id: id, capture_ids: [id], source_origin: "third_party" },
};

beforeEach(() => mockedInvoke.mockReset());

function responses(text: string, count: number): void {
  let index = 0;
  const bytes = new TextEncoder().encode(text).length;
  mockedInvoke.mockImplementation(async () => {
    const current = index++;
    return { status: "ok", dto_version: 1, record, content: { kind: "untrusted_text", text },
      start_byte: current * bytes, end_byte: (current + 1) * bytes,
      complete: index === count, next_cursor: index === count ? null : `cursor-${index}` };
  });
}

it("refuses encoded aggregate overflow including Unicode and response metadata", async () => {
  responses("🙂".repeat(16000), 263);
  await expect(readCompleteRecord(record)).rejects.toBe("response_too_large");
  expect(mockedInvoke.mock.calls.length).toBeLessThanOrEqual(263);
});

it("refuses a single response beyond the encoded envelope budget", async () => {
  responses("🙂".repeat(300000), 1);
  await expect(readCompleteRecord(record)).rejects.toBe("response_too_large");
  expect(mockedInvoke).toHaveBeenCalledOnce();
});

it("stops before the 501st read call", async () => {
  responses("x", 501);
  await expect(readCompleteRecord(record)).rejects.toBe("response_too_large");
  expect(mockedInvoke).toHaveBeenCalledTimes(500);
});

it("reconstructs valid Unicode through the bounded complete read", async () => {
  responses("🙂中e\u0301", 3);
  await expect(readCompleteRecord(record)).resolves.toMatchObject({ text: "🙂中e\u0301".repeat(3) });
  expect(mockedInvoke).toHaveBeenCalledTimes(3);
});
