import { createHash } from "node:crypto";
import { chmod, copyFile, lstat, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { flattenInto, planFlatten, readArchiveInventory, sha256, verifyFlattened } from "./flatten-base.mjs";

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
  ["base", "libexec/open-brain/open-brain"],
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
  resources.get(resourcePlatform).set(role, { archiveSha256, executableSha256, filename });
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

// The one-folder base ships as a copy with every link replaced by its resolved target, since
// Tauri resources cannot carry symlinks. The released base archive, bound by the manifest, is the
// inventory authority; the folder being copied and every output file are verified against it.
const baseFolder = path.dirname(coreSource);
const baseName = path.basename(coreSource);
if (!(await lstat(path.join(baseFolder, "_internal")).then((info) => info.isDirectory(), () => false))) {
  throw new Error("the desktop runtime requires the one-folder base");
}
const baseArchive = await readFile(path.join(path.dirname(releaseManifest), components.get("base").filename)).catch(
  () => { throw new Error("the base release archive is unavailable"); },
);
if (createHash("sha256").update(baseArchive).digest("hex") !== components.get("base").archiveSha256) {
  throw new Error("base archive digest does not match the release manifest");
}
const plan = planFlatten(readArchiveInventory(baseArchive));
if (plan.files.get(baseName)?.sha256 !== components.get("base").executableSha256) {
  throw new Error("core executable digest does not match the release manifest");
}
const graphifyDigest = await sha256(graphifySource);
if (graphifyDigest !== components.get("graphify").executableSha256) {
  throw new Error("graphify executable digest does not match the release manifest");
}

await mkdir(outputDirectory, { recursive: true, mode: 0o700 });
// The output directory also holds .gitkeep; only the runtime tree and manifest are ours.
await rm(path.join(outputDirectory, "runtime"), { force: true, recursive: true });
await rm(path.join(outputDirectory, "desktop-component-manifest-v1.json"), { force: true });
const coreFile = "runtime/libexec/open-brain/open-brain";
const graphifyFile = "runtime/libexec/open-brain-graphify";
try {
  await flattenInto(baseFolder, plan, path.join(outputDirectory, "runtime/libexec/open-brain"));
  await verifyFlattened(plan, path.join(outputDirectory, "runtime/libexec/open-brain"));
  const graphifyDestination = path.join(outputDirectory, graphifyFile);
  await copyFile(graphifySource, graphifyDestination);
  await chmod(graphifyDestination, 0o700);
  if (await sha256(graphifyDestination) !== graphifyDigest) {
    throw new Error("graphify executable copy does not match the release manifest");
  }
} catch (error) {
  await rm(path.join(outputDirectory, "runtime"), { force: true, recursive: true });
  throw error;
}
const prepared = [
  { role: "core", file: coreFile, sha256: plan.files.get(baseName).sha256 },
  { role: "graphify", file: graphifyFile, sha256: graphifyDigest },
];

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
