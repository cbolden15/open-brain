"""Component manifest v2 release: prefix-relative archives and the v2 Homebrew formula.

A release that includes the embedding role uses manifest v2 (``EMBEDDING-RESOURCE.md``
section 3); v1 manifests and their formula stay in ``base_native``. Every v2
archive holds final installed prefix-relative paths plus its own inventory, so the formula
copies each staged tree into the keg exactly per that inventory.

Homebrew rewrites Mach-O and ELF linkage in the keg after ``install`` (``fix_dynamic_linkage``),
which would change the audited embedding runtime bytes. The formula therefore verifies the
embedding tree during ``install`` but keeps only its downloaded archive, and copies the tree
into place in ``post_install``, which runs after that rewrite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from tools.open_brain_dev.component_manifest_v2 import (
    EXECUTABLES,
    MANIFEST_FILENAME,
    PLATFORMS,
    ComponentManifestError,
    Inventory,
    ManifestV2,
    Member,
    ResourceV2,
    archive_filename,
    inventory_path,
    render_inventory,
    render_manifest_v2,
    validate_resource_inventory,
)

_PLATFORM_BLOCKS: Final = {
    "linux-x86_64": ("on_linux", "x86_64"),
    "macos-arm64": ("on_macos", "arm64"),
}
PENDING_EMBEDDING: Final = "open-brain-embedding.pending.tar.gz"
# v1 archive member prefix -> installed prefix-relative path (the v1 formula's install rules).
V1_LAYOUTS: Final = {
    "base": (
        ("open-brain/", "libexec/open-brain/"),
        ("obsidian-plugin/", "share/open-brain/obsidian-plugin/"),
    ),
    "graphify": (
        ("open-brain", "libexec/open-brain-graphify"),
        ("licenses/graphify/", "share/open-brain/licenses/graphify/"),
    ),
}
# The formula's bin.install_symlink for the one-folder base, as an inventory member.
V1_ENTRY_LINKS: Final = {"base": (("bin/open-brain", "../libexec/open-brain/open-brain"),)}
_V1_MEMBER_LIMIT: Final = 512 * 1024 * 1024


class ReleaseV2Error(RuntimeError):
    """A v2 release archive or manifest cannot be produced."""


def _v1_location(role: str, name: str) -> str:
    for source, target in V1_LAYOUTS[role]:
        if name == source or (source.endswith("/") and name.startswith(source)):
            return target + name[len(source) :] if source.endswith("/") else target
    raise ReleaseV2Error(f"{role} archive member has no v2 location")


def _v1_members(
    archive: Path, role: str
) -> tuple[list[tuple[str, int, bytes]], list[tuple[str, str]], list[str]]:
    """Files with their exec bit, relative links and directories at v2 locations."""
    files: list[tuple[str, int, bytes]] = []
    links: list[tuple[str, str]] = list(V1_ENTRY_LINKS.get(role, ()))
    directories: list[str] = []
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            for info in bundle.getmembers():
                if info.isdir():
                    if info.name + "/" not in {source for source, _ in V1_LAYOUTS[role]}:
                        directories.append(_v1_location(role, info.name))
                    continue
                if info.issym():
                    # Containment is enforced when the inventory is rendered below.
                    links.append((_v1_location(role, info.name), info.linkname))
                    continue
                if not info.isreg() or info.size > _V1_MEMBER_LIMIT:
                    raise ReleaseV2Error(f"{role} archive member is not a regular file")
                path = _v1_location(role, info.name)
                stream = bundle.extractfile(info)
                if stream is None:
                    raise ReleaseV2Error(f"{role} archive member is unreadable")
                files.append((path, 0o755 if info.mode & 0o111 else 0o644, stream.read()))
    except (OSError, tarfile.TarError) as error:
        raise ReleaseV2Error(f"{role} archive cannot be read") from error
    return files, links, directories


def _canonical_record(resource: ResourceV2) -> bytes:
    document = {
        "role": resource.role,
        "platform": resource.platform,
        "filename": resource.filename,
        "archive_sha256": resource.archive_sha256,
        "archive_bytes": resource.archive_bytes,
        "executable": resource.executable,
        "executable_sha256": resource.executable_sha256,
        "inventory": resource.inventory,
        "inventory_sha256": resource.inventory_sha256,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def convert_v1_archive(
    archive: Path, *, role: str, version: str, platform: str, output: Path
) -> ResourceV2:
    """Rewrite an audited v1 base or Graphify archive at v2 prefix-relative paths."""
    from tools.open_brain_dev.embedding_native import _directories, _File, _write_archive

    found, links, archived = _v1_members(archive, role)
    files = sorted(found)
    paths = [path for path, _, _ in files]
    linked = [path for path, _ in links]
    if EXECUTABLES[role] not in paths or len({*paths, *linked}) != len(paths) + len(linked):
        raise ReleaseV2Error(f"{role} archive does not hold exactly one executable")
    directories = sorted(
        set(_directories([*paths, *linked, inventory_path(role)])) | set(archived)
    )
    members = sorted(
        [
            *(Member(path, "directory", 0o755, 0, None) for path in directories),
            *(
                Member(path, "file", mode, len(data), hashlib.sha256(data).hexdigest())
                for path, mode, data in files
            ),
            *(Member(path, "symlink", 0o777, 0, None, target) for path, target in links),
        ],
        key=lambda member: member.path,
    )
    try:
        inventory_bytes = render_inventory(Inventory(version, role, platform, tuple(members), None))
    except ComponentManifestError as error:
        raise ReleaseV2Error(f"{role} inventory is invalid: {error}") from error
    output.mkdir(parents=True, exist_ok=True)
    target = output / archive_filename(role, version, platform)
    _write_archive(
        target,
        [
            *(_File(path, mode, data) for path, mode, data in files),
            _File(inventory_path(role), 0o644, inventory_bytes),
        ],
        directories,
        links,
    )
    archive_bytes = target.read_bytes()
    executable = next(data for path, _, data in files if path == EXECUTABLES[role])
    resource = ResourceV2(
        role=role,
        platform=platform,
        filename=target.name,
        archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
        archive_bytes=len(archive_bytes),
        executable=EXECUTABLES[role],
        executable_sha256=hashlib.sha256(executable).hexdigest(),
        inventory=inventory_path(role),
        inventory_sha256=hashlib.sha256(inventory_bytes).hexdigest(),
    )
    validate_resource_inventory(
        resource, version, Inventory(version, role, platform, tuple(members), None)
    )
    target.with_name(target.name.removesuffix(".tar.gz") + ".resource.json").write_bytes(
        _canonical_record(resource)
    )
    return resource


def mirror_for_smoke(resources: Sequence[ResourceV2], version: str) -> tuple[ResourceV2, ...]:
    """Contributor smoke only: stand in host records for the platform that was not built.

    A v2 manifest requires both platforms, but one host builds only its own resources. The
    mirrored records are never fetched (Homebrew evaluates only the host block) and the
    renderer marks the formula as the keg-only smoke fixture; releases never use this.
    """
    platforms = {item.platform for item in resources}
    if len(platforms) != 1:
        raise ReleaseV2Error("smoke mirroring needs exactly one built platform")
    (built,) = platforms
    (absent,) = set(PLATFORMS) - platforms
    mirrored = [
        ResourceV2(
            role=item.role,
            platform=absent,
            filename=archive_filename(item.role, version, absent),
            archive_sha256=item.archive_sha256,
            archive_bytes=item.archive_bytes,
            executable=item.executable,
            executable_sha256=item.executable_sha256,
            inventory=item.inventory,
            inventory_sha256=item.inventory_sha256,
        )
        for item in resources
        if item.platform == built
    ]
    return (*resources, *mirrored)


def write_manifest_v2(resources: Sequence[ResourceV2], version: str, destination: Path) -> Path:
    from tools.open_brain_dev.component_manifest_v2 import ROLES

    ordered = tuple(sorted(resources, key=lambda item: (item.platform, ROLES.index(item.role))))
    try:
        data = render_manifest_v2(ManifestV2(version, ordered))
    except ComponentManifestError as error:
        raise ReleaseV2Error(f"manifest v2 is invalid: {error}") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return destination


_HELPERS: Final = """\
  def component_records
    require "json"
    platform = OS.mac? ? "macos-arm64" : "linux-x86_64"
    JSON.parse(COMPONENT_MANIFEST_V2)["resources"]
        .select { |item| item["platform"] == platform }
        .to_h { |item| [item["role"], item] }
  end

  # Copy one staged v2 tree exactly per its inventory; with no destination only verify it.
  def install_component(directory, record, destination = nil)
    require "digest"
    require "json"
    role = record["role"]
    inventory_path = File.join(directory, record["inventory"])
    inventory_bytes = File.binread(inventory_path)
    if Digest::SHA256.hexdigest(inventory_bytes) != record["inventory_sha256"]
      odie "open-brain: #{role} inventory does not match the component manifest"
    end
    declared = { record["inventory"] => true }
    JSON.parse(inventory_bytes)["members"].each do |member|
      path = member["path"]
      declared[path] = true
      source = File.join(directory, path)
      info = File.lstat(source)
      if member["kind"] == "directory"
        odie "open-brain: #{role} member #{path} is not a directory" unless info.directory?
      elsif member["kind"] == "symlink"
        unless info.symlink? && File.readlink(source) == member["target"]
          odie "open-brain: #{role} member #{path} does not match its inventory"
        end
      elsif !info.file? || info.nlink != 1 || info.size != member["bytes"] ||
            Digest::SHA256.file(source).hexdigest != member["sha256"]
        odie "open-brain: #{role} member #{path} does not match its inventory"
      end
      next if destination.nil?

      target = destination/path
      if member["kind"] == "symlink"
        # The inventory parser proved the relative target resolves to a declared member.
        File.symlink(member["target"], target)
        next
      end

      if member["kind"] == "directory"
        target.mkpath
      else
        FileUtils.cp source, target
      end
      target.chmod member["mode"]
    end
    Dir.glob("**/*", File::FNM_DOTMATCH, base: directory).each do |path|
      next if [".", ".."].include?(File.basename(path)) || path.start_with?(".brew_home")
      odie "open-brain: #{role} archive has an undeclared entry #{path}" unless declared[path]
    end
    return if destination.nil?

    FileUtils.cp inventory_path, destination/record["inventory"]
    (destination/record["inventory"]).chmod 0644
  end

  # Before extraction: only relative entries the inventory declares, links with their target.
  def verify_archive(archive, record)
    require "digest"
    require "json"
    require "rubygems/package"
    require "zlib"
    role = record["role"]
    entries = {}
    inventory_bytes = nil
    Zlib::GzipReader.open(archive) do |gzip|
      Gem::Package::TarReader.new(gzip).each do |entry|
        path = entry.full_name.delete_suffix("/")
        if path.split("/", -1).any? { |part| ["", ".", ".."].include?(part) } ||
           !(entry.file? || entry.directory? || entry.symlink?)
          odie "open-brain: #{role} archive has an unsafe entry #{path}"
        end
        entries[path] = if entry.directory?
          "directory"
        elsif entry.symlink?
          "symlink:#{entry.header.linkname}"
        else
          "file"
        end
        inventory_bytes = entry.read if path == record["inventory"]
      end
    end
    if inventory_bytes.nil? ||
       Digest::SHA256.hexdigest(inventory_bytes) != record["inventory_sha256"]
      odie "open-brain: #{role} archive inventory does not match the component manifest"
    end
    kinds = { record["inventory"] => "file" }
    JSON.parse(inventory_bytes)["members"].each do |member|
      kind = member["kind"]
      kinds[member["path"]] = kind == "symlink" ? "symlink:#{member["target"]}" : kind
    end
    entries.each do |path, kind|
      odie "open-brain: #{role} archive has an undeclared entry #{path}" unless kinds[path] == kind
    end
  end
"""


def _resource(name: str, origin: str, filename: str, sha256: str) -> tuple[str, ...]:
    return (
        f'    resource "{name}" do',
        f'      url "{origin}/{filename}"',
        f'      sha256 "{sha256}"',
        "    end",
    )


def render_homebrew_formula_v2(
    manifest: ManifestV2, *, origin: str, repository: str, smoke: bool
) -> str:
    """Render the formula for a v2 manifest; ``origin`` is already validated."""
    document = render_manifest_v2(manifest).decode("utf-8")
    roles = {item.role for item in manifest.resources}
    lines = [
        "class OpenBrainSmoke < Formula" if smoke else "class OpenBrain < Formula",
        '  desc "Local-first capture, search, and portable export"',
        f'  homepage "https://github.com/{repository}"',
        f'  version "{manifest.version}"',
        '  license "Apache-2.0"',
        "",
    ]
    if smoke:
        lines.extend(('  keg_only "Temporary contributor smoke fixture"', ""))
    for platform_tag in sorted({item.platform for item in manifest.resources}):
        resources = {
            item.role: item for item in manifest.resources if item.platform == platform_tag
        }
        base = resources["base"]
        block, architecture = _PLATFORM_BLOCKS[platform_tag]
        lines.extend(
            (
                f"  {block} do",
                f"    depends_on arch: :{architecture}",
                f'    url "{origin}/{base.filename}"',
                f'    sha256 "{base.archive_sha256}"',
            )
        )
        for role in ("graphify", "harness", "embedding"):
            item = resources.get(role)
            if item is not None:
                lines.extend(_resource(role, origin, item.filename, item.archive_sha256))
        lines.extend(("  end", ""))
    lines.extend(
        (
            "  COMPONENT_MANIFEST_V2 = <<~'JSON'.freeze",
            f"    {document.rstrip(chr(10))}",
            "  JSON",
            "",
            "  def install",
            "    records = component_records",
            '    install_component(buildpath, records.fetch("base"), prefix)',
            '    resource("graphify").stage do',
            '      install_component(Pathname.pwd, records.fetch("graphify"), prefix)',
            "    end",
        )
    )
    if "harness" in roles:
        lines.extend(
            (
                '    resource("harness").stage do',
                '      install_component(Pathname.pwd, records.fetch("harness"), prefix)',
                "    end",
            )
        )
    lines.extend(
        (
            '    resource("embedding").stage do',
            '      install_component(Pathname.pwd, records.fetch("embedding"))',
            "    end",
            f'    pending = libexec/"{PENDING_EMBEDDING}"',
            '    FileUtils.cp resource("embedding").cached_download, pending',
            f'    (share/"open-brain/{MANIFEST_FILENAME}").write COMPONENT_MANIFEST_V2',
            "  end",
            "",
            "  def post_install",
            f'    pending = libexec/"{PENDING_EMBEDDING}"',
            "    return unless pending.exist?",
            "",
            '    verify_archive(pending, component_records.fetch("embedding"))',
            '    stage = prefix/".open-brain-embedding-stage"',
            "    rm_r stage if stage.exist?",
            "    stage.mkpath",
            '    system "tar", "-xzf", pending, "-C", stage',
            '    install_component(stage, component_records.fetch("embedding"), prefix)',
            "    rm_r stage",
            "    rm pending",
            "  end",
            "",
            _HELPERS.rstrip("\n"),
            "",
            "  test do",
            '    assert_match "open-brain #{version}", shell_output("#{bin}/open-brain --version")',
            '    assert_path_exists share/"open-brain/obsidian-plugin/main.js"',
            (
                '    assert_match "open-brain-graphify-helper-v1", '
                'shell_output("#{libexec}/open-brain-graphify --capabilities")'
            ),
        )
    )
    if "harness" in roles:
        lines.extend(
            (
                '    assert_path_exists share/"open-brain/harness/harness-hook.mjs"',
                '    assert_match "harness", '
                'shell_output("#{libexec}/open-brain-harness/open-brain-harness harness --help")',
            )
        )
    lines.extend(
        (
            '    require "digest"',
            '    require "json"',
            # The staged component.json is the canonical inventory component object (D9).
            '    inventory = (share/"open-brain/inventories/embedding.json").binread',
            '    component = inventory[/\\A\\{"component":(\\{.*\\}),"members":\\[/m, 1]',
            '    stage = testpath/"embedding-stage"',
            "    stage.mkpath",
            '    (stage/"component.json").binwrite component',
            '    cp_r libexec/"open-brain-embedding/tokenizer", stage',
            "    request = JSON.generate({",
            '      protocol:   "open-brain.embedding-helper.v1",',
            '      request_id: "#{"0" * 31}1",',
            '      operation:  "capabilities",',
            '    }) + "\\n"',
            '    helper = (libexec/"open-brain-embedding/open-brain-embedding").to_s',
            '    output = IO.popen([helper, "--stdio-v1"], "r+", chdir: stage.to_s) do |io|',
            "      io.write request",
            "      io.close_write",
            "      io.read",
            "    end",
            "    assert_predicate $CHILD_STATUS, :success?",
            "    capabilities = JSON.parse(output)",
            '    assert_equal "ok", capabilities["status"]',
            '    assert_equal Digest::SHA256.hexdigest(component), capabilities["component_id"]',
            "  end",
            "end",
            "",
        )
    )
    return "\n".join(lines)


def smoke_release(release: Path, embedding_archive: Path, output: Path) -> Path:
    """Write the host's v2 base/Graphify archives, link the embedding archive and a manifest."""
    from tools.open_brain_dev.base_native import native_platform_tag
    from tools.open_brain_dev.embedding_native import verify_embedding_archive

    version, embedding, _ = verify_embedding_archive(embedding_archive)
    platform = native_platform_tag()
    if embedding.platform != platform:
        raise ReleaseV2Error("embedding archive is not for this host")
    resources = [
        convert_v1_archive(
            release
            / f"open-brain{'' if role == 'base' else '-' + role}-{version}-{platform}.tar.gz",
            role=role,
            version=version,
            platform=platform,
            output=output,
        )
        for role in ("base", "graphify")
    ]
    # Link rather than copy the large archive; the smoke directory is deleted afterwards.
    os.symlink(embedding_archive.resolve(strict=True), output / embedding.filename)
    resources.append(embedding)
    return write_manifest_v2(
        mirror_for_smoke(resources, version), version, output / MANIFEST_FILENAME
    )


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.open_brain_dev.release_v2")
    subparsers = parser.add_subparsers(dest="command", required=True)
    smoke = subparsers.add_parser("smoke-release")
    smoke.add_argument("--release", type=Path, required=True)
    smoke.add_argument("--embedding-archive", type=Path, required=True)
    smoke.add_argument("--output", type=Path, required=True)
    namespace = parser.parse_args(argv)
    try:
        manifest = smoke_release(
            namespace.release.resolve(strict=True),
            namespace.embedding_archive,
            namespace.output.resolve(strict=True),
        )
    except (ReleaseV2Error, ComponentManifestError, OSError) as error:
        print(f"release-v2: {error}", file=sys.stderr)
        return 2
    print(os.fspath(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
