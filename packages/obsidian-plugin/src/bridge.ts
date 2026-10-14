import { parseStrictJson } from "./strict-json";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomUUID } from "node:crypto";
import { realpath, stat } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

import {
  OPEN_BRAIN_CLIENT_PROTOCOL,
  OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
  type BridgeRequest,
  type PluginOperation,
  record,
} from "./contracts";
import {
  FACET_OPERATIONS, RESOLVE_OPERATION, encodeFacetRequest, negotiateFacets,
  parseFacetResponseFrame, type FacetBridgeOperation, type FacetNegotiation,
} from "./facet-wire";

const MAX_REQUEST_BYTES = 64 * 1024;
const MAX_RESPONSE_BYTES = 5 * 1024 * 1024;
const MAX_STDERR_BYTES = 16 * 1024;
const INTEGER_WIRE_OPERATIONS = new Set<string>([
  "contract.describe", "search.page", "record.read", "history.list", "history.show",
  "source.route", "relationship.decide", "relationship.list", "decision.history",
]);

type Pending = {
  operation: PluginOperation;
  reject: (error: Error) => void;
  resolve: (value: unknown) => void;
  timer: ReturnType<typeof setTimeout>;
  maximumResponseBytes: number;
};

type SpawnProcess = (
  executable: string,
  args: readonly string[],
  options: {
    detached: boolean;
    env: NodeJS.ProcessEnv;
    shell: false;
    stdio: ["pipe", "pipe", "pipe"];
    windowsHide: true;
  },
) => ChildProcessWithoutNullStreams;

export class BridgeError extends Error {
  public constructor(public readonly code: string) {
    super(code);
    this.name = "BridgeError";
  }
}

export class OpenBrainBridge {
  readonly #executable: string;
  readonly #spawn: SpawnProcess;
  #child: ChildProcessWithoutNullStreams | null = null;
  #pending = new Map<string, Pending>();
  #stdout = Buffer.alloc(0);
  #stderrBytes = 0;
  #disposed = false;
  #facetNegotiation: FacetNegotiation | null = null;
  #facetHandshake: Promise<unknown> | null = null;

  public constructor(executable: string, spawnProcess: SpawnProcess = spawn) {
    if (!path.isAbsolute(executable)) throw new BridgeError("binary_unavailable");
    this.#executable = executable;
    this.#spawn = spawnProcess;
  }

  public async invoke<T>(
    operation: PluginOperation,
    args: Record<string, unknown>,
    timeoutMs = 15_000,
    requestId = `plugin_${randomUUID()}`,
  ): Promise<T> {
    if (this.#disposed) throw new BridgeError("bridge_closed");
    if (!/^plugin_[0-9a-f-]{36}$/.test(requestId) || timeoutMs < 1 || timeoutMs > 120_000) {
      throw new BridgeError("invalid_request");
    }
    const facet = isFacetOperation(operation);
    if (facet && this.#facetNegotiation === null) {
      this.#facetHandshake ??= this.invoke("system.handshake", {}, timeoutMs).finally(() => {
        this.#facetHandshake = null;
      });
      await this.#facetHandshake;
    }
    const negotiated = this.#facetNegotiation;
    const bounds = facet && negotiated?.status === "available" ? negotiated.limits[operation] : undefined;
    if (facet && bounds === undefined) throw new BridgeError("unsupported_capability");
    const child = this.#ensureChild();
    const request: BridgeRequest = {
      arguments: args,
      operation,
      protocol: OPEN_BRAIN_CLIENT_PROTOCOL,
      protocol_version: OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
      request_id: requestId,
    };
    const payload = facet ? Buffer.from(encodeFacetRequest(operation, args, requestId))
      : Buffer.from(`${JSON.stringify(request)}\n`, "utf8");
    if (payload.length > (bounds?.request_bytes ?? MAX_REQUEST_BYTES) || this.#pending.has(requestId)) {
      throw new BridgeError("invalid_request");
    }
    return await new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(requestId);
        reject(new BridgeError("timeout"));
        this.#terminate();
      }, timeoutMs);
      this.#pending.set(requestId, {
        operation,
        reject,
        resolve: (value) => resolve(value as T),
        timer,
        maximumResponseBytes: bounds?.response_bytes ?? (
          INTEGER_WIRE_OPERATIONS.has(operation) ? 1024 * 1024 : MAX_RESPONSE_BYTES
        ),
      });
      child.stdin.write(payload, (error) => {
        if (error !== null && error !== undefined) {
          if (this.#child !== child) return;
          const pending = this.#pending.get(requestId);
          if (pending !== undefined) {
            clearTimeout(pending.timer);
            this.#pending.delete(requestId);
            pending.reject(new BridgeError("transport_failed"));
          }
          this.#terminate();
        }
      });
    });
  }

  public dispose(): void {
    this.#disposed = true;
    this.#terminate();
  }

  /** Cancel in-flight work while keeping this client reusable for a fresh child session. */
  public cancelPending(): void {
    if (!this.#disposed) this.#terminate();
  }

  #ensureChild(): ChildProcessWithoutNullStreams {
    if (this.#child !== null && this.#child.exitCode === null) return this.#child;
    const child = this.#spawn(this.#executable, ["plugin"], {
      detached: true,
      env: filteredEnvironment(process.env),
      shell: false,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    this.#child = child;
    this.#stdout = Buffer.alloc(0);
    this.#stderrBytes = 0;
    child.stdout.on("data", (chunk: Buffer | string) => {
      if (this.#child === child) this.#onStdout(chunk);
    });
    child.stderr.on("data", (chunk: Buffer | string) => {
      if (this.#child !== child) return;
      this.#stderrBytes += Buffer.byteLength(chunk);
      if (this.#stderrBytes > MAX_STDERR_BYTES) this.#terminate();
    });
    child.once("error", () => {
      if (this.#child !== child) return;
      this.#child = null;
      this.#facetNegotiation = null;
      this.#failAll("transport_failed");
    });
    child.once("exit", () => {
      if (this.#child !== child) return;
      this.#child = null;
      this.#facetNegotiation = null;
      this.#failAll("bridge_closed");
    });
    return child;
  }

  #onStdout(chunk: Buffer | string): void {
    const incoming = Buffer.from(chunk);
    let offset = 0;
    while (offset < incoming.length) {
      const newline = incoming.indexOf(0x0a, offset);
      const end = newline < 0 ? incoming.length : newline + 1;
      const maximum = Math.max(1, ...Array.from(this.#pending.values(), (p) => p.maximumResponseBytes));
      if (this.#stdout.length + end - offset > maximum) {
        this.#failAll("response_too_large"); this.#terminate(); return;
      }
      this.#stdout = Buffer.concat([this.#stdout, incoming.subarray(offset, end)]);
      offset = end;
      if (newline >= 0) {
        const line = this.#stdout.subarray(0, this.#stdout.length - 1);
        this.#stdout = Buffer.alloc(0);
        this.#handleLine(line);
        if (this.#pending.size === 0 && offset < incoming.length) {
          this.#terminate(); return;
        }
      }
    }
  }

  #handleLine(line: Buffer): void {
    let response: Record<string, unknown>;
    try {
      response = record(parseStrictJson(line));
    } catch {
      this.#failAll("protocol_error");
      this.#terminate();
      return;
    }
    const requestId = response.request_id;
    if (
      response.protocol !== OPEN_BRAIN_CLIENT_PROTOCOL ||
      response.protocol_version !== OPEN_BRAIN_CLIENT_PROTOCOL_VERSION ||
      typeof requestId !== "string"
    ) {
      this.#failAll("protocol_error");
      this.#terminate();
      return;
    }
    const pending = this.#pending.get(requestId);
    if (pending === undefined) {
      this.#failAll("protocol_error");
      this.#terminate();
      return;
    }
    if (line.length + 1 > pending.maximumResponseBytes) {
      this.#failAll("response_too_large"); this.#terminate(); return;
    }
    if (isFacetOperation(pending.operation)) {
      clearTimeout(pending.timer); this.#pending.delete(requestId);
      let validLexemes = false;
      try {
        parseStrictJson(line, true);
        validLexemes = true;
        pending.resolve(parseFacetResponseFrame(pending.operation, Buffer.concat([line, Buffer.from("\n")]), requestId));
      } catch (error) {
        const fields = Object.keys(response).sort().join(",");
        const closedError = validLexemes && response.ok === false && fields === "error,ok,protocol,protocol_version,request_id"
          && typeof response.error === "object" && response.error !== null
          && Object.keys(response.error).join(",") === "code"
          && error instanceof Error && "code" in error
          && error.code === (response.error as { code?: unknown }).code;
        pending.reject(closedError && error instanceof Error ? error : new BridgeError("protocol_error"));
      }
      return;
    }
    if (INTEGER_WIRE_OPERATIONS.has(pending.operation) || ["publication.show", "publication.list"].includes(pending.operation)) {
      try {
        if (INTEGER_WIRE_OPERATIONS.has(pending.operation) && line.length + 1 > 1024 * 1024) throw new Error("response_too_large");
        // Validate the original lexemes before JSON's number conversion can erase them.
        parseStrictJson(line, true);
      } catch {
        this.#failAll("protocol_error");
        this.#terminate();
        return;
      }
    }
    clearTimeout(pending.timer);
    this.#pending.delete(requestId);
    if (response.ok === true && "result" in response && !("error" in response)) {
      if (pending.operation === "system.handshake") this.#facetNegotiation = negotiateFacets(response.result);
      pending.resolve(response.result);
      return;
    }
    if (response.ok === false && "error" in response && !("result" in response)) {
      try {
        const error = record(response.error);
        if (typeof error.code !== "string" || error.code.length === 0) throw new Error();
        pending.reject(new BridgeError(error.code));
        return;
      } catch {
        // Fall through to one public protocol failure.
      }
    }
    pending.reject(new BridgeError("protocol_error"));
    this.#terminate();
  }

  #failAll(code: string): void {
    for (const pending of this.#pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new BridgeError(code));
    }
    this.#pending.clear();
  }

  #terminate(): void {
    const child = this.#child;
    this.#child = null;
    this.#facetNegotiation = null;
    this.#failAll("bridge_closed");
    if (child === null || child.exitCode !== null) return;
    child.stdin.end();
    if (child.pid !== undefined) {
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch {
        child.kill("SIGTERM");
      }
      const force = setTimeout(() => {
        if (child.exitCode === null) {
          try {
            process.kill(-child.pid!, "SIGKILL");
          } catch {
            child.kill("SIGKILL");
          }
        }
      }, 1_000);
      (force as { unref?: () => void }).unref?.();
    } else {
      child.kill("SIGTERM");
    }
  }
}

function isFacetOperation(operation: PluginOperation): operation is FacetBridgeOperation {
  return operation === RESOLVE_OPERATION || (FACET_OPERATIONS as readonly string[]).includes(operation);
}

export async function discoverOpenBrainExecutable(explicitPath: string): Promise<string> {
  const candidates = executableCandidates(explicitPath, process.platform, process.env.HOME);
  for (const candidate of candidates) {
    try {
      const resolved = await realpath(candidate);
      const metadata = await stat(resolved);
      if (metadata.isFile() && (metadata.mode & 0o111) !== 0) return resolved;
    } catch {
      if (explicitPath.length > 0) break;
    }
  }
  throw new BridgeError("binary_unavailable");
}

export function executableCandidates(
  explicitPath: string,
  platform: NodeJS.Platform,
  home: string | undefined,
): string[] {
  if (explicitPath.length > 0) return path.isAbsolute(explicitPath) ? [explicitPath] : [];
  if (platform === "darwin") {
    return ["/opt/homebrew/bin/open-brain", "/usr/local/bin/open-brain"];
  }
  if (platform === "linux") {
    const linuxbrewExecutable = path.join(
      path.sep,
      "home",
      "linuxbrew",
      ".linuxbrew",
      "bin",
      "open-brain",
    );
    return [
      ...(home === undefined ? [] : [path.join(home, ".linuxbrew/bin/open-brain")]),
      linuxbrewExecutable,
      "/usr/local/bin/open-brain",
      "/usr/bin/open-brain",
    ];
  }
  return [];
}

export function filteredEnvironment(source: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const allowed = [
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "WAYLAND_DISPLAY",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
  ];
  const result: NodeJS.ProcessEnv = {};
  for (const key of allowed) {
    const value = source[key];
    if (value !== undefined) result[key] = value;
  }
  return result;
}
