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
  filteredEnvironment,
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
      protocol: "open-brain-client",
      protocol_version: 1,
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

  it("T03 rejects duplicate nested keys before response construction", async () => {
    const executable = await fakeExecutable(`
process.stdin.once("data", (chunk) => {
  const request = JSON.parse(chunk);
  const raw = JSON.stringify({ok:true, protocol:"open-brain-client", protocol_version:1, request_id:request.request_id, result:{role:1}});
  process.stdout.write(raw.replace('"role":1', '"role":1,"role":2') + "\\n");
});
`);
    const bridge = new OpenBrainBridge(executable);
    bridges.push(bridge);
    await expect(bridge.invoke("system.handshake", {})).rejects.toEqual(
      expect.objectContaining<Partial<BridgeError>>({code:"protocol_error"}),
    );
  });

  it("kills the owned session when a request exceeds its deadline", async () => {
    const executable = await fakeExecutable(`process.stdin.resume();`);
    const bridge = new OpenBrainBridge(executable);
    bridges.push(bridge);

    await expect(bridge.invoke("system.handshake", {}, 25)).rejects.toEqual(
      expect.objectContaining<Partial<BridgeError>>({ code: "timeout" }),
    );
  });

  it.each([2, undefined])("rejects a response with protocol version %s", async (version) => {
    const executable = await fakeExecutable(`
process.stdin.setEncoding("utf8");
process.stdin.once("data", (chunk) => {
  const request = JSON.parse(chunk);
  const response = {
    ok: true,
    protocol: "open-brain-client",
    request_id: request.request_id,
    result: {},
  };
  if (${version === undefined ? "false" : "true"}) response.protocol_version = ${version ?? 0};
  process.stdout.write(JSON.stringify(response) + "\\n");
});
`);
    const bridge = new OpenBrainBridge(executable);
    bridges.push(bridge);

    await expect(bridge.invoke("system.handshake", {})).rejects.toEqual(
      expect.objectContaining<Partial<BridgeError>>({ code: "protocol_error" }),
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
      path.join(path.sep, "home", "test", ".linuxbrew", "bin", "open-brain"),
    );
  });

  it("passes desktop session routing without ambient provider credentials", () => {
    const environment = filteredEnvironment({
      DBUS_SESSION_BUS_ADDRESS: "unix:path=/run/user/1000/bus",
      HOME: "/home/test",
      OPENAI_API_KEY: "must-not-pass",
      XDG_RUNTIME_DIR: "/run/user/1000",
    });

    expect(environment).toEqual({
      DBUS_SESSION_BUS_ADDRESS: "unix:path=/run/user/1000/bus",
      HOME: "/home/test",
      XDG_RUNTIME_DIR: "/run/user/1000",
    });
  });
});

async function fakeExecutable(body: string): Promise<string> {
  const directory = await mkdtemp(path.join(tmpdir(), "open-brain-plugin-test-"));
  const executable = path.join(directory, "open-brain");
  await writeFile(executable, `#!${process.execPath}\n${body}\n`, "utf8");
  await chmod(executable, 0o700);
  return executable;
}
