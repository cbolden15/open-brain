/** Real source child/engine proof. Proxy modes alter transport bytes only. */
import { chmod, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import { OpenBrainBridge } from "../src/bridge";
import { FacetClient } from "../src/facet-client";
import { parseFacetPublicationInspection } from "../src/facet-inspection";
import type { PluginOperation } from "../src/contracts";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const executable = path.join(root, ".venv/bin/open-brain");
const bridges: OpenBrainBridge[] = [];
const directories: string[] = [];

afterEach(async () => {
  for (const bridge of bridges.splice(0)) bridge.dispose();
  for (const directory of directories.splice(0)) await rm(directory, {recursive: true, force: true});
});

type ProxyMode = "plain" | "drop-once" | "malform-once" | "malformed-error-once" | "small-request" | "small-response" | "inspection-exponent";

async function live(mode: ProxyMode = "plain") {
  const directory = await realpath(await mkdtemp(path.join(tmpdir(), "synthetic-open-brain-facet-")));
  directories.push(directory);
  const dataDir = path.join(directory, "synthetic-brain");
  const marker = path.join(directory, "transport-fault-used");
  const wrapper = path.join(directory, "source-plugin-wrapper");
  // This wrapper is part of the test fixture only. Every domain operation goes
  // through the source executable with an explicit synthetic --data-dir.
  await writeFile(wrapper, `#!${process.execPath}
const {spawn} = require("node:child_process");
const fs = require("node:fs");
const mode = ${JSON.stringify(mode)};
const requests = new Map();
const child = spawn(${JSON.stringify(executable)}, [...process.argv.slice(2), "--data-dir", ${JSON.stringify(dataDir)}], {
  cwd: ${JSON.stringify(root)}, env: {...process.env, PYTHONPATH: ${JSON.stringify([
    "packages/app/src", "packages/engine/src", "packages/collector/src", "packages/connectors/src",
  ].map((part) => path.join(root, part)).join(path.delimiter))}}, stdio: ["pipe", "pipe", "pipe"]});
let incoming = "", outgoing = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  incoming += chunk;
  let newline;
  while ((newline = incoming.indexOf("\\n")) >= 0) {
    const raw = incoming.slice(0, newline + 1); incoming = incoming.slice(newline + 1);
    const request = JSON.parse(raw); requests.set(request.request_id, request.operation);
    child.stdin.write(raw);
  }
});
process.stdin.on("end", () => child.stdin.end());
child.stderr.pipe(process.stderr);
child.stdout.setEncoding("utf8");
child.stdout.on("data", (chunk) => {
  outgoing += chunk;
  let newline;
  while ((newline = outgoing.indexOf("\\n")) >= 0) {
    const raw = outgoing.slice(0, newline); outgoing = outgoing.slice(newline + 1);
    const response = JSON.parse(raw); const operation = requests.get(response.request_id);
    if (response.ok && operation === "system.handshake" && mode.startsWith("small-")) {
      for (const entry of response.result.facet_capabilities.operations) {
        if (mode === "small-request" && entry.operation === "facet.dimension.create") entry.request_bytes = 240;
        if (mode === "small-response" && entry.operation === "facet.dimension.list") entry.response_bytes = 200;
      }
    }
    if (response.ok && operation === "facet.dimension.create" && (mode === "drop-once" || mode === "malform-once" || mode === "malformed-error-once") && !fs.existsSync(${JSON.stringify(marker)})) {
      fs.writeFileSync(${JSON.stringify(marker)}, "used");
      if (mode === "drop-once") continue;
      if (mode === "malformed-error-once") {
        response.ok = false;
        delete response.result;
        response.error = {code: "invalid_arguments"};
        process.stdout.write(JSON.stringify(response).replace('"protocol_version":1', '"protocol_version":1e0') + "\\n");
        continue;
      }
      response.result.unexpected_field = true;
    }
    if (response.ok && operation === "publication.show" && mode === "inspection-exponent") {
      process.stdout.write(JSON.stringify(response).replace('"schema_version":5', '"schema_version":5e0') + "\\n");
    } else process.stdout.write(JSON.stringify(response) + "\\n");
  }
});
child.on("exit", (code) => process.exit(code ?? 1));
process.on("SIGTERM", () => { child.kill("SIGTERM"); process.exit(0); });
`, "utf8");
  await chmod(wrapper, 0o700);
  const bridge = new OpenBrainBridge(wrapper);
  bridges.push(bridge);
  await bridge.invoke("brain.initialize", {});
  const handshake = await bridge.invoke("system.handshake", {});
  const boundedTransport = {invoke: <T,>(operation: PluginOperation, args: Record<string, unknown>, timeout?: number) =>
    bridge.invoke<T>(operation, args, operation === "facet.dimension.create" && mode === "drop-once" ? 1500 : timeout)};
  return {bridge, client: new FacetClient(boundedTransport, handshake), directory, dataDir};
}

describe("actual OpenBrainBridge child against synthetic source runtime", () => {
  it("O03/O05: vocabulary persists across real child restart and renegotiation", async () => {
    const {bridge, client} = await live();
    const mutation = client.createMutation("facet.dimension.create", {dto_version: 1, expected_version: 0, name: "研究🙂 café"});
    const receipt = await mutation.submit();
    expect(receipt.status).toBe("applied");
    bridge.cancelPending();
    const listed = await client.call("facet.dimension.list", {dto_version: 1});
    expect(listed.items).toEqual([{dimension_id: receipt.subject_id, name: "研究🙂 café", archived: false, version: 1}]);
    expect((await bridge.invoke<{facet_capabilities: unknown}>("system.handshake", {})).facet_capabilities).toBeDefined();
  }, 20_000);

  it.each(["drop-once", "malform-once", "malformed-error-once"] as const)(
    "O05: %s after real commit stays uncertain and retries the same durable operation", async (mode) => {
      const {bridge, client} = await live(mode);
      const mutation = client.createMutation("facet.dimension.create", {dto_version: 1, expected_version: 0, name: "Synthetic uncertain mutation"});
      const original = JSON.stringify(mutation.request);
      await expect(mutation.submit()).rejects.toMatchObject({code: mode === "drop-once" ? "timeout" : "protocol_error"});
      expect(mutation.status).toBe("uncertain");
      // Force a real new child even when the parser, rather than a deadline, failed.
      bridge.cancelPending();
      const receipt = await mutation.retry();
      expect(receipt.operation_id).toBe(mutation.request.operation_id);
      expect(JSON.stringify(mutation.request)).toBe(original);
      const listed = await client.call("facet.dimension.list", {dto_version: 1});
      expect(listed.items).toHaveLength(1);
      expect(listed.items[0]!.dimension_id).toBe(receipt.subject_id);
      const history = await client.call("facet.history.list", {dto_version: 1,
        subject: {kind: "dimension", id: receipt.subject_id}});
      expect(history.items).toHaveLength(1);
    }, 20_000);

  it("O12: smaller negotiated request bound rejects before the actual child mutation", async () => {
    const {client} = await live("small-request");
    const mutation = client.createMutation("facet.dimension.create", {dto_version: 1, expected_version: 0,
      name: "Synthetic request that exceeds the runtime's smaller negotiated frame bound"});
    await expect(mutation.submit()).rejects.toMatchObject({code: "invalid_request"});
    expect((await client.call("facet.dimension.list", {dto_version: 1})).items).toEqual([]);
  }, 20_000);

  it("O12: smaller negotiated response bound limits actual child buffering", async () => {
    const {client} = await live("small-response");
    await client.createMutation("facet.dimension.create", {dto_version: 1, expected_version: 0, name: "Synthetic"}).submit();
    await expect(client.call("facet.dimension.list", {dto_version: 1})).rejects.toMatchObject({code: "response_too_large"});
  }, 20_000);

  it.each(["plain", "inspection-exponent"] as const)(
    "O07/O12: %s inspection uses actual frozen evidence and strict wire integers", async (mode) => {
      const {bridge, client} = await live(mode);
      const dimension = await client.createMutation("facet.dimension.create", {dto_version: 1, expected_version: 0, name: "Synthetic project"}).submit();
      const value = await client.createMutation("facet.value.create", {dto_version: 1, expected_version: 0,
        expected_dimension_version: 1, dimension_id: dimension.subject_id, name: "Synthetic value"}).submit();
      const capture = await bridge.invoke<{capture_id: string}>("capture.create", {text: "Synthetic source evidence"});
      await client.createMutation("facet.assignment.patch", {dto_version: 1, record_id: capture.capture_id,
        expected_revision_id: capture.capture_id, expected_version: 0, add_value_ids: [value.subject_id], remove_value_ids: []}).submit();
      const query = await client.createMutation("facet.query.create", {dto_version: 1, expected_version: 0,
        name: "Synthetic saved query", selector: {all_value_ids: [value.subject_id], record_types: ["source"]}}).submit();
      await bridge.invoke("space.create", {dto_version: 1, name: "Synthetic destination", idempotency_key: "synthetic-destination"});
      const spaces = await bridge.invoke<{spaces: {space_id: string}[]}>("space.list", {dto_version: 1});
      const proposal = await client.createMutation("review.propose_from_facet_query", {dto_version: 1,
        query_id: query.subject_id, expected_query_version: 1, target_page_id: null,
        expected_page_revision_id: null, destination_space_id: spaces.spaces[0]!.space_id,
        title: "Synthetic publication", markdown: "Owner-written synthetic draft"}).submit();
      const request = bridge.invoke("publication.show", {dto_version: 1, proposal_id: proposal.proposal_id});
      if (mode === "inspection-exponent") {
        await expect(request).rejects.toMatchObject({code: "protocol_error"});
      } else {
        const inspection = parseFacetPublicationInspection(await request);
        expect(inspection.selected_capture_ids).toEqual([capture.capture_id]);
        expect(inspection.facet_selection?.query_id).toBe(query.subject_id);
      }
    }, 20_000);
});
