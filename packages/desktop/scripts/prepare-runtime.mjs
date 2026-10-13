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

const manifestText = await readFile(releaseManifest, "utf8");
if (/[^\x00-\x7f]/.test(manifestText)) {
  throw new Error("invalid Open Brain component manifest encoding");
}
const lines = manifestText.split(/\r\n|\n|\r/);
if (lines.at(-1) === "") lines.pop();
if (lines[0] !== "open-brain-component-manifest-v1") {
  throw new Error("invalid Open Brain component manifest");
}
const versionFields = lines[1]?.split(" ");
if (versionFields?.length !== 2 || versionFields[0] !== "version"
    || !/^[0-9A-Za-z][0-9A-Za-z._-]*$/.test(versionFields[1])) {
  throw new Error("invalid Open Brain component version");
}
const destinations = new Map([
  ["base", "bin/open-brain"],
  ["graphify", "libexec/open-brain-graphify"],
  ["harness", "libexec/open-brain-harness/open-brain-harness"],
]);
const roles = ["base", "graphify", "harness"];
const resources = new Map();
let previousKey = "";
for (const line of lines.slice(2)) {
  const fields = line.split(" ");
  const [record, role, resourcePlatform, archiveSha256, executableSha256, filename, destination] = fields;
  const archive = filename?.match(
    /^open-brain-(?:(graphify|harness)-)?([0-9A-Za-z][0-9A-Za-z._-]*)-(linux-x86_64|macos-arm64)\.tar\.gz$/,
  );
  if (fields.length !== 7 || record !== "resource" || !destinations.has(role)
      || !Object.values(platformByTarget).includes(resourcePlatform)
      || !/^[0-9a-f]{64}$/.test(archiveSha256)
      || !/^[0-9a-f]{64}$/.test(executableSha256)
      || destination !== destinations.get(role) || archive === null || archive === undefined
      || (archive[1] ?? "base") !== role || archive[2] !== versionFields[1]
      || archive[3] !== resourcePlatform) {
    throw new Error("invalid Open Brain component resource");
  }
  const key = `${resourcePlatform}:${roles.indexOf(role)}`;
  if (key <= previousKey) {
    throw new Error("duplicate or noncanonical Open Brain component resource");
  }
  previousKey = key;
  if (!resources.has(resourcePlatform)) resources.set(resourcePlatform, new Map());
  resources.get(resourcePlatform).set(role, { executableSha256 });
}
const resourceSets = new Set([...resources.values()].map((items) => [...items.keys()].join(",")));
if (resourceSets.size !== 1
    || !["base,graphify", "base,graphify,harness"].includes([...resourceSets][0])) {
  throw new Error("invalid Open Brain component resource sets");
}
const components = resources.get(platform);
if (!components?.has("base") || !components.has("graphify")) {
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
