/** Client-state proof only. Durable authority and restore are covered by Python/live transport. */
import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import type { OpenBrainBridge } from "../src/bridge";
import { FacetClient } from "../src/facet-client";
import type { FacetRequestMap } from "../src/facet-types";

type Transport = Pick<OpenBrainBridge, "invoke">;
const fixtures = JSON.parse(readFileSync(new URL("fixtures/facets/o0-cases.json", import.meta.url), "utf8"));
// Reuse frozen facet DTOs under current runtime admission; keep fixture bytes historical.
fixtures.handshake = { ...fixtures.handshake, state_schema_version: 16, runtime_session_version: 11 };
const cases = JSON.parse(readFileSync(new URL("../../engine/tests/fixtures/facets/wire-cases.json", import.meta.url), "utf8"));
const positive = (operation: string) => cases.cases.find((row: {operation: string; outcome: string}) =>
  row.operation === operation && row.outcome === "accept");
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const code = (value: string) => Object.assign(new Error(value), {code: value});
const transport = (invoke: ReturnType<typeof vi.fn>) => ({invoke} as unknown as Transport);

describe("FacetClient capability and lifecycle states (unit proof)", () => {
  it("O01: old runtime disables facets while legacy invocation remains usable", async () => {
    const invoke = vi.fn().mockResolvedValue({status: "legacy"});
    const client = new FacetClient(transport(invoke), {protocol: "open-brain-client",
      protocol_version: 1, operations: ["capture.create", "search.query"]});
    expect(client.supports("facet.dimension.create")).toBe(false);
    await expect(client.call("facet.dimension.list", {dto_version: 1})).rejects.toMatchObject({code: "unsupported_capability"});
    expect(invoke).not.toHaveBeenCalled();
    await expect(client.invoke("search.query", {query: "Synthetic", limit: 1})).resolves.toEqual({status: "legacy"});
  });

  it("O01: partial capability intersection is binding", () => {
    const handshake = copy(fixtures.handshake);
    handshake.operations = ["facet.dimension.list"];
    const client = new FacetClient(transport(vi.fn()), handshake);
    expect(client.supports("facet.dimension.list")).toBe(true);
    expect(client.supports("facet.dimension.create")).toBe(false);
    client.dispose();
    expect(client.supports("facet.dimension.list")).toBe(false);
  });

  it("O05: duplicate submission shares one promise, retry preserves the entire request", async () => {
    let release!: (value: unknown) => void;
    const invoke = vi.fn().mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    const client = new FacetClient(transport(invoke), fixtures.handshake);
    const mutation = client.createMutation("facet.dimension.create", {dto_version: 1, expected_version: 0, name: "Synthetic"});
    const first = mutation.submit();
    const duplicate = mutation.submit();
    expect(duplicate).toBe(first);
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(mutation.status).toBe("pending");
    const receipt = {...positive("facet.dimension.create").response, operation_id: mutation.request.operation_id};
    release(receipt);
    await expect(first).resolves.toEqual(receipt);
    expect(mutation.status).toBe("committed");
    await expect(mutation.retry()).resolves.toEqual(receipt);
    expect(invoke).toHaveBeenCalledTimes(1);
  });

  it.each(["timeout", "bridge_closed", "transport_failed", "operation_pending"])(
    "O05: uncertain %s retains identity and arguments for an explicit retry", async (failure) => {
      const invoke = vi.fn().mockRejectedValueOnce(code(failure));
      const client = new FacetClient(transport(invoke), fixtures.handshake);
      const mutation = client.createMutation("facet.dimension.create", {dto_version: 1, expected_version: 0, name: "Synthetic"});
      const original = copy(mutation.request);
      await expect(mutation.submit()).rejects.toMatchObject({code: failure});
      expect(mutation.status).toBe("uncertain");
      const receipt = {...positive("facet.dimension.create").response, operation_id: original.operation_id};
      invoke.mockResolvedValueOnce(receipt);
      await mutation.retry();
      expect(invoke.mock.calls[0]![1]).toEqual(original);
      expect(invoke.mock.calls[1]![1]).toEqual(original);
      expect(mutation.status).toBe("committed");
    });

  it("O05: nested request is copied and deeply frozen before confirmation", () => {
    const invoke = vi.fn();
    const client = new FacetClient(transport(invoke), fixtures.handshake);
    const fixture = positive("facet.query.create");
    const args = copy(fixture.request) as FacetRequestMap["facet.query.create"];
    const mutation = client.createMutation("facet.query.create", args);
    const expected = copy(mutation.request);
    (args.selector.all_value_ids as string[]).push("facetval_00000000-0000-4000-8000-000000000099");
    expect(mutation.request).toEqual(expected);
    expect(Object.isFrozen(mutation.request)).toBe(true);
    expect(Object.isFrozen(mutation.request.selector)).toBe(true);
    expect(Object.isFrozen(mutation.request.selector.all_value_ids)).toBe(true);
    expect(() => (mutation.request.selector.all_value_ids as string[]).push("changed")).toThrow();
    expect(invoke).not.toHaveBeenCalled();
  });

  it("O04: confirmed conflict cannot overwrite state by retrying the stale mutation", async () => {
    const invoke = vi.fn().mockRejectedValue(code("version_conflict"));
    const client = new FacetClient(transport(invoke), fixtures.handshake);
    const fixture = positive("facet.assignment.patch");
    const mutation = client.createMutation("facet.assignment.patch", fixture.request);
    await expect(mutation.submit()).rejects.toMatchObject({code: "version_conflict"});
    expect(mutation.status).toBe("conflict");
    await expect(mutation.retry()).rejects.toMatchObject({code: "version_conflict"});
    expect(invoke).toHaveBeenCalledTimes(1);
    const replacement = client.createMutation("facet.assignment.patch", {...fixture.request, expected_version: 2});
    expect(replacement.request.operation_id).not.toBe(mutation.request.operation_id);
  });

  it("O13: disposal rejects late facet reads and prevents replacement writes", async () => {
    let release!: (value: unknown) => void;
    const invoke = vi.fn().mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    const client = new FacetClient(transport(invoke), fixtures.handshake);
    const pending = client.call("facet.dimension.list", {dto_version: 1});
    client.dispose();
    release(positive("facet.dimension.list").response);
    await expect(pending).rejects.toMatchObject({code: "bridge_closed"});
    await expect(client.call("facet.dimension.create", positive("facet.dimension.create").request))
      .rejects.toMatchObject({code: "unsupported_capability"});
    expect(invoke).toHaveBeenCalledTimes(1);
  });

  it("O13: disposal rejects late managed-record resolution", async () => {
    let release!: (value: unknown) => void;
    const invoke = vi.fn().mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    const client = new FacetClient(transport(invoke), fixtures.handshake);
    const pending = client.resolve(fixtures.managed_request.workspace_id, fixtures.managed_request.relative_path);
    client.dispose();
    release({status: "ok", dto_version: 1, workspace_id: fixtures.managed_request.workspace_id,
      record_id: "page_00000000-0000-4000-8000-000000000001", revision_id: "revision_00000000-0000-4000-8000-000000000002",
      subject: {kind: "canonical", id: "page_00000000-0000-4000-8000-000000000001"}});
    await expect(pending).rejects.toMatchObject({code: "bridge_closed"});
  });

  it("O13: disposal rejects late legacy invocation without disposing the shared transport", async () => {
    let release!: (value: unknown) => void;
    const invoke = vi.fn().mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    const client = new FacetClient(transport(invoke), fixtures.handshake);
    const pending = client.invoke("workspace.status", {});
    client.dispose();
    release({status: "ok"});
    await expect(pending).rejects.toMatchObject({code: "bridge_closed"});
    expect(invoke).toHaveBeenCalledTimes(1);
  });
});
