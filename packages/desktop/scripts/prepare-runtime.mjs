import { createHash } from "node:crypto";
import { chmod, copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const [coreSource, graphifySource, releaseManifest, outputDirectory, target] = process.argv.slice(2);
if ([coreSource, graphifySource, releaseManifest, outputDirectory, target].some((value) => !value)) {
  throw new Error(
    "usage: prepare-runtime.mjs <core> <graphify> <manifest> <output-directory> <target>",
  );
}

const platformByTarget = {
  "aarch64-apple-darwin": "macos-arm64",
  "x86_64-unknown-linux-gnu": "linux-x86_64",
};
const platform = platformByTarget[target];
if (platform === undefined) throw new Error(`unsupported desktop target: ${target}`);

const lines = (await readFile(releaseManifest, "ascii")).trimEnd().split("\n");
if (lines[0] !== "open-brain-component-manifest-v1") {
  throw new Error("invalid Open Brain component manifest");
}
const versionFields = lines[1]?.split(" ");
if (versionFields?.length !== 2 || versionFields[0] !== "version") {
  throw new Error("invalid Open Brain component version");
}
const components = new Map();
for (const line of lines.slice(2)) {
  const fields = line.split(" ");
  if (fields.length !== 7 || fields[0] !== "resource" || fields[2] !== platform) continue;
  components.set(fields[1], { executableSha256: fields[4] });
}
if (!components.has("base") || !components.has("graphify") || components.size !== 2) {
  throw new Error("the desktop runtime requires one matched base and Graphify pair");
}

await mkdir(outputDirectory, { recursive: true, mode: 0o700 });
const prepared = [
  { role: "core", source: coreSource, file: "runtime/bin/open-brain", expected: components.get("base") },
  {
    role: "graphify",
    source: graphifySource,
    file: "runtime/libexec/open-brain-graphify",
    expected: components.get("graphify"),
  },
];
for (const item of prepared) {
  const digest = await sha256(item.source);
  if (digest !== item.expected.executableSha256) {
    throw new Error(`${item.role} executable digest does not match the release manifest`);
  }
  const destination = path.join(outputDirectory, item.file);
  await mkdir(path.dirname(destination), { recursive: true, mode: 0o700 });
  await copyFile(item.source, destination);
  await chmod(destination, 0o700);
  item.sha256 = digest;
}

const desktopManifest = {
  components: prepared.map(({ file, role, sha256 }) => ({ file, role, sha256 })),
  core_version: versionFields[1],
  desktop_version: "0.1.0",
  protocol: "open-brain-client",
  protocol_version: 1,
  schema_version: 1,
  target,
};
await writeFile(
  path.join(outputDirectory, "desktop-component-manifest-v1.json"),
  `${JSON.stringify(desktopManifest)}\n`,
  { encoding: "ascii", mode: 0o600 },
);

async function sha256(file) {
  return createHash("sha256").update(await readFile(file)).digest("hex");
}
