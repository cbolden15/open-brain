import { createHash } from "node:crypto";
import { chmod, copyFile, lstat, mkdir, readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { gunzipSync } from "node:zlib";

// Tauri resources dereference file symlinks and cannot copy directory symlinks, so the desktop
// ships a copy of the one-folder base in which every link is replaced by a regular copy of its
// resolved target. The rules mirror the base inventory: relative, contained, at most four hops,
// no cycles. Every output file is then verified against the hash of the member it came from.

const MAX_HOPS = 4;

// The members come from the released base archive (reproducible USTAR, bound by the manifest's
// archive digest), not from the folder being copied, so the shipped tree is checked against the
// release. Only members below `open-brain/` are the base; plugin assets are skipped.
export function readArchiveInventory(archive) {
  const tar = gunzipSync(archive);
  const members = new Map();
  const text = (start, length) => {
    const field = tar.subarray(start, start + length);
    const end = field.indexOf(0);
    return field.subarray(0, end === -1 ? length : end).toString("latin1");
  };
  const octal = (start, length) => {
    const value = Number.parseInt(text(start, length).trim(), 8);
    if (!Number.isSafeInteger(value) || value < 0) throw new Error("base archive header is invalid");
    return value;
  };
  let offset = 0;
  while (offset + 512 <= tar.length) {
    if (tar.subarray(offset, offset + 512).every((byte) => byte === 0)) break;
    const prefix = text(offset + 345, 155);
    const name = (prefix === "" ? "" : `${prefix}/`) + text(offset, 100);
    const type = String.fromCharCode(tar[offset + 156] || 48);
    const size = octal(offset + 124, 12);
    const mode = octal(offset + 100, 8) & 0o777;
    const linkname = text(offset + 157, 100);
    const payload = tar.subarray(offset + 512, offset + 512 + size);
    offset += 512 + Math.ceil(size / 512) * 512;
    if (offset > tar.length) throw new Error("base archive is truncated");
    const trimmed = name.replace(/\/$/, "");
    if (trimmed === "open-brain" || trimmed === "obsidian-plugin"
        || trimmed.startsWith("obsidian-plugin/")) continue;
    if (!trimmed.startsWith("open-brain/")) throw new Error(`unexpected base archive member: ${name}`);
    const member = trimmed.slice("open-brain/".length);
    if (members.has(member)) throw new Error(`duplicate base archive member: ${member}`);
    if (type === "5") {
      members.set(member, { kind: "dir", path: member });
    } else if (type === "2") {
      members.set(member, { kind: "symlink", path: member, target: linkname });
    } else if (type === "0") {
      members.set(member, {
        kind: "file",
        mode,
        path: member,
        sha256: createHash("sha256").update(payload).digest("hex"),
      });
    } else {
      throw new Error(`unsupported base archive member: ${name}`);
    }
  }
  return members;
}

export function planFlatten(members) {
  const plan = { dirs: new Set(), files: new Map() };
  emitDirectory("", "", []);
  return plan;

  function children(directory) {
    return [...members.values()].filter(
      (member) => path.posix.dirname(member.path) === (directory === "" ? "." : directory),
    );
  }

  function emitDirectory(source, destination, stack) {
    if (stack.includes(source)) throw new Error(`base link cycle through ${source}`);
    if (destination !== "") plan.dirs.add(destination);
    for (const child of children(source)) {
      const target = destination === "" ? path.posix.basename(child.path)
        : `${destination}/${path.posix.basename(child.path)}`;
      emitMember(child, target, [...stack, source]);
    }
  }

  function emitMember(member, destination, stack) {
    const resolved = member.kind === "symlink" ? resolveLink(member) : member;
    if (resolved.kind === "dir") {
      emitDirectory(resolved.path, destination, stack);
    } else {
      plan.files.set(destination, {
        mode: resolved.mode,
        sha256: resolved.sha256,
        source: resolved.path,
      });
    }
  }

  function resolveLink(link) {
    const seen = new Set();
    let hops = 0;
    // The release audit only allows links below _internal/ that resolve below _internal/.
    if (!link.path.startsWith("_internal/")) {
      throw new Error(`base link ${link.path} is outside _internal`);
    }
    const resolved = path.posix.dirname(link.path).split("/").filter((part) => part !== ".");
    let parts = expand(link);
    while (parts.length > 0) {
      const part = parts.shift();
      if (part === "..") {
        if (resolved.length <= 1) throw new Error(`base link ${link.path} escapes _internal`);
        resolved.pop();
        continue;
      }
      const candidate = [...resolved, part].join("/");
      const member = members.get(candidate);
      if (member === undefined) throw new Error(`base link ${link.path} has no target`);
      if (member.kind === "symlink") {
        parts = [...expand(member), ...parts];
        continue;
      }
      resolved.push(part);
    }
    const final = members.get(resolved.join("/"));
    if (final === undefined || resolved.length === 0 || resolved[0] !== "_internal") {
      throw new Error(`base link ${link.path} has no target`);
    }
    return final;

    function expand(symlink) {
      if (seen.has(symlink.path)) throw new Error(`base link cycle through ${symlink.path}`);
      seen.add(symlink.path);
      hops += 1;
      if (hops > MAX_HOPS) throw new Error(`base link ${link.path} exceeds ${MAX_HOPS} hops`);
      validateTarget(symlink);
      return symlink.target.split("/");
    }
  }
}

function validateTarget(symlink) {
  const { target } = symlink;
  const parts = target.split("/");
  let leading = true;
  const valid = target !== "" && !target.startsWith("/") && !target.includes("\0")
    && !target.includes("\\")
    && parts.every((part) => {
      if (part === "" || part === ".") return false;
      if (part !== "..") leading = false;
      return part !== ".." || leading;
    });
  if (!valid) throw new Error(`base link ${symlink.path} has an unsafe target`);
}

export async function flattenInto(root, plan, output) {
  await mkdir(output, { recursive: true, mode: 0o700 });
  for (const directory of [...plan.dirs].sort()) {
    await mkdir(path.join(output, directory), { recursive: true, mode: 0o700 });
  }
  for (const [destination, entry] of plan.files) {
    const file = path.join(output, destination);
    await copyFile(path.join(root, entry.source), file);
    await chmod(file, entry.mode & 0o111 ? 0o700 : 0o600);
  }
}

export async function verifyFlattened(plan, output) {
  const seenDirectories = new Set();
  const seenFiles = new Set();
  await walk("");
  if (seenDirectories.size !== plan.dirs.size || [...plan.dirs].some((d) => !seenDirectories.has(d))) {
    throw new Error("flattened base directories differ from the inventory");
  }
  if (seenFiles.size !== plan.files.size || [...plan.files.keys()].some((f) => !seenFiles.has(f))) {
    throw new Error("flattened base files differ from the inventory");
  }

  async function walk(directory) {
    for (const name of (await readdir(path.join(output, directory))).sort()) {
      const member = directory === "" ? name : `${directory}/${name}`;
      const absolute = path.join(output, member);
      const info = await lstat(absolute);
      if (info.isSymbolicLink()) throw new Error(`flattened base keeps a symlink: ${member}`);
      if (info.isDirectory()) {
        seenDirectories.add(member);
        await walk(member);
      } else if (info.isFile()) {
        const expected = plan.files.get(member);
        if (expected === undefined) throw new Error(`flattened base has an extra file: ${member}`);
        if (await sha256(absolute) !== expected.sha256
            || ((info.mode & 0o111) !== 0) !== ((expected.mode & 0o111) !== 0)) {
          throw new Error(`flattened base file does not match the inventory: ${member}`);
        }
        seenFiles.add(member);
      } else {
        throw new Error(`flattened base has an unsupported member: ${member}`);
      }
    }
  }
}

export async function sha256(file) {
  return createHash("sha256").update(await readFile(file)).digest("hex");
}
