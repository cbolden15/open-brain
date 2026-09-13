import { chmod, mkdtemp, realpath, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";

import { afterEach, describe, expect, it } from "vitest";

import {
  BridgeError,
  OpenBrainBridge,
  discoverOpenBrainExecutable,
  executableCandidates,
} from "../src/bridge";

const bridges: OpenBrainBridge[] = [];

afterEach(() => {
  for (const bridge of bridges) bridge.dispose();
  bridges.length = 0;
});

describe("OpenBrainBridge", () => {
  it("discovers one explicit executable and keeps a session for multiple requests", async () => {
    const executable = await fakeExecutable(`
let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let newline = buffer.indexOf("\\n");
  while (newline >= 0) {
    const request = JSON.parse(buffer.slice(0, newline));
    buffer = buffer.slice(newline + 1);
    process.stdout.write(JSON.stringify({
      ok: true,
      protocol: "open-brain-plugin-v1",
      request_id: request.request_id,
      result: { operation: request.operation },
    }) + "\\n");
    newline = buffer.indexOf("\\n");
  }
});
`);
    expect(await discoverOpenBrainExecutable(executable)).toBe(await realpath(executable));
    const bridge = new OpenBrainBridge(executable);
    bridges.push(bridge);

    const first = await bridge.invoke<{ operation: string }>("system.handshake", {});
    const second = await bridge.invoke<{ operation: string }>("workspace.status", {});

    expect(first.operation).toBe("system.handshake");
    expect(second.operation).toBe("workspace.status");
  });

  it("kills the owned session when a request exceeds its deadline", async () => {
    const executable = await fakeExecutable(`process.stdin.resume();`);
    const bridge = new OpenBrainBridge(executable);
    bridges.push(bridge);

    await expect(bridge.invoke("system.handshake", {}, 25)).rejects.toEqual(
      expect.objectContaining<Partial<BridgeError>>({ code: "timeout" }),
    );
  });
});

describe("executableCandidates", () => {
  it("does not use PATH and rejects relative overrides", () => {
    expect(executableCandidates("open-brain", "darwin", "/Users/test")).toEqual([]);
    expect(executableCandidates("", "darwin", "/Users/test")).toEqual([
      "/opt/homebrew/bin/open-brain",
      "/usr/local/bin/open-brain",
    ]);
    expect(executableCandidates("", "linux", "/home/test")[0]).toBe(
      "/home/test/.linuxbrew/bin/open-brain",
    );
  });
});

async function fakeExecutable(body: string): Promise<string> {
  const directory = await mkdtemp(path.join(tmpdir(), "open-brain-plugin-test-"));
  const executable = path.join(directory, "open-brain");
  await writeFile(executable, `#!${process.execPath}\n${body}\n`, "utf8");
  await chmod(executable, 0o700);
  return executable;
}
