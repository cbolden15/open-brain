import { randomUUID } from "node:crypto";
import type { OpenBrainBridge } from "./bridge";
import type { PluginOperation } from "./contracts";
import {
  negotiateFacets, parseFacetRequest, parseFacetResponse, parseManagedRecordResponse,
  FacetWireError, type FacetBridgeOperation, type FacetNegotiation,
  type ManagedRecordResponse,
} from "./facet-wire";
import type { FacetOperation, FacetRequestMap, FacetResponseMap } from "./facet-types";

export type FacetMutationStatus = "ready" | "pending" | "uncertain" | "committed" | "conflict";
type Transport = Pick<OpenBrainBridge, "invoke">;

/** One loaded plugin session. It retains no mutation content in settings. */
export class FacetClient {
  public readonly negotiation: FacetNegotiation;
  #closed = false;
  public constructor(public readonly bridge: Transport, handshake: unknown) {
    this.negotiation = negotiateFacets(handshake);
  }
  public supports(operation: FacetBridgeOperation): boolean {
    return !this.#closed && this.negotiation.status === "available"
      && this.negotiation.operations.includes(operation);
  }
  public async call<O extends FacetOperation>(operation: O, args: FacetRequestMap[O]): Promise<FacetResponseMap[O]> {
    if (!this.supports(operation)) throw new FacetWireError("unsupported_capability");
    const request = parseFacetRequest(operation, JSON.stringify(args));
    const result = await this.bridge.invoke(operation as PluginOperation, request as Record<string, unknown>);
    if (this.#closed) throw new FacetWireError("bridge_closed");
    try { return parseFacetResponse(operation, JSON.stringify(result)); }
    catch { throw new FacetWireError("protocol_error"); }
  }
  public createMutation<O extends FacetOperation>(operation: O, args: Omit<FacetRequestMap[O], "operation_id">): FacetMutation<O> {
    return new FacetMutation(this, operation, { ...args, operation_id: `obsidian-facet-${randomUUID()}` } as FacetRequestMap[O]);
  }
  public async resolve(workspaceId: string, relativePath: string): Promise<ManagedRecordResponse> {
    if (!this.supports("workspace.record.resolve")) throw new FacetWireError("unsupported_capability");
    const result = await this.bridge.invoke("workspace.record.resolve" as PluginOperation,
      { dto_version: 1, workspace_id: workspaceId, relative_path: relativePath });
    if (this.#closed) throw new FacetWireError("bridge_closed");
    try { return parseManagedRecordResponse(result); }
    catch { throw new FacetWireError("protocol_error"); }
  }
  public async invoke<T = unknown>(operation: PluginOperation, args: Record<string, unknown>, timeoutMs?: number): Promise<T> {
    if (this.#closed) throw new FacetWireError("bridge_closed");
    const result = await this.bridge.invoke<T>(operation, args, timeoutMs);
    if (this.#closed) throw new FacetWireError("bridge_closed");
    return result;
  }
  public dispose(): void { this.#closed = true; }
}

/** Retains the exact confirmed request across explicit retries and child restarts. */
export class FacetMutation<O extends FacetOperation> {
  public status: FacetMutationStatus = "ready";
  public result: FacetResponseMap[O] | null = null;
  public readonly request: FacetRequestMap[O];
  #pending: Promise<FacetResponseMap[O]> | null = null;
  public constructor(private readonly client: FacetClient, public readonly operation: O, request: FacetRequestMap[O]) {
    this.request = freezeRequest(JSON.parse(JSON.stringify(request))) as FacetRequestMap[O];
  }
  public submit(): Promise<FacetResponseMap[O]> {
    if (this.#pending !== null) return this.#pending;
    if (this.status === "committed" && this.result !== null) return Promise.resolve(this.result);
    if (this.status === "conflict") return Promise.reject(new FacetWireError("version_conflict"));
    this.status = "pending";
    this.#pending = this.client.call(this.operation, this.request).then((result) => {
      this.result = result; this.status = "committed"; return result;
    }).catch((error: unknown) => {
      const code = typeof error === "object" && error !== null && "code" in error ? String(error.code) : "operation_pending";
      this.status = ["version_conflict", "revision_changed", "review_conflict", "preview_stale",
        "projection_conflict", "facet_archived", "query_archived", "name_conflict",
        "invalid_arguments", "unsupported_capability", "not_found", "idempotency_conflict",
        "limit_exceeded"].includes(code) ? "conflict" : "uncertain";
      throw error;
    }).finally(() => { this.#pending = null; });
    return this.#pending;
  }
  public retry(): Promise<FacetResponseMap[O]> { return this.submit(); }
}

function freezeRequest(value: unknown): unknown {
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value)) freezeRequest(child);
    Object.freeze(value);
  }
  return value;
}
