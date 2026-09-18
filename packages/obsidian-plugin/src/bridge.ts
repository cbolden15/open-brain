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

const MAX_REQUEST_BYTES = 64 * 1024;
const MAX_RESPONSE_BYTES = 5 * 1024 * 1024;
const MAX_STDERR_BYTES = 16 * 1024;

type Pending = {
  reject: (error: Error) => void;
  resolve: (value: unknown) => void;
  timer: ReturnType<typeof setTimeout>;
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
    const child = this.#ensureChild();
    const request: BridgeRequest = {
      arguments: args,
      operation,
      protocol: OPEN_BRAIN_CLIENT_PROTOCOL,
      protocol_version: OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
      request_id: requestId,
    };
    const payload = Buffer.from(`${JSON.stringify(request)}\n`, "utf8");
    if (payload.length > MAX_REQUEST_BYTES || this.#pending.has(requestId)) {
      throw new BridgeError("invalid_request");
    }
    return await new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(requestId);
        reject(new BridgeError("timeout"));
        this.#terminate();
      }, timeoutMs);
      this.#pending.set(requestId, {
        reject,
        resolve: (value) => resolve(value as T),
        timer,
      });
      child.stdin.write(payload, (error) => {
        if (error !== null && error !== undefined) {
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
    child.stdout.on("data", (chunk: Buffer | string) => this.#onStdout(chunk));
    child.stderr.on("data", (chunk: Buffer | string) => {
      this.#stderrBytes += Buffer.byteLength(chunk);
      if (this.#stderrBytes > MAX_STDERR_BYTES) this.#terminate();
    });
    child.once("error", () => this.#failAll("transport_failed"));
    child.once("exit", () => {
      if (this.#child === child) this.#child = null;
      this.#failAll("bridge_closed");
    });
    return child;
  }

  #onStdout(chunk: Buffer | string): void {
    this.#stdout = Buffer.concat([this.#stdout, Buffer.from(chunk)]);
    if (this.#stdout.length > MAX_RESPONSE_BYTES) {
      this.#failAll("response_too_large");
      this.#terminate();
      return;
    }
    let newline = this.#stdout.indexOf(0x0a);
    while (newline >= 0) {
      const line = this.#stdout.subarray(0, newline);
      this.#stdout = this.#stdout.subarray(newline + 1);
      this.#handleLine(line);
      newline = this.#stdout.indexOf(0x0a);
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
    clearTimeout(pending.timer);
    this.#pending.delete(requestId);
    if (response.ok === true && "result" in response && !("error" in response)) {
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
      force.unref();
    } else {
      child.kill("SIGTERM");
    }
  }
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
