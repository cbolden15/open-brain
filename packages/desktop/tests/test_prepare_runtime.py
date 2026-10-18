"""Desktop selects its pair from the actual native release manifest contract."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from tools.open_brain_dev import base_native

DESKTOP = Path(__file__).resolve().parents[1]
TARGETS = {
    "aarch64-apple-darwin": "macos-arm64",
    "x86_64-unknown-linux-gnu": "linux-x86_64",
}
DESTINATIONS = {
    "base": "libexec/open-brain/open-brain",
    "graphify": "libexec/open-brain-graphify",
    "harness": "libexec/open-brain-harness/open-brain-harness",
}


def write_base(folder: Path) -> Path:
    """A one-folder base shaped like the real one: EXE, `_internal`, file and directory links."""
    internal = folder / "_internal"
    framework = internal / "Python.framework"
    (framework / "Versions/3.14/Resources").mkdir(parents=True)
    (folder / "open-brain").write_bytes(b"synthetic-core")
    (folder / "open-brain").chmod(0o755)
    (internal / "base_library.zip").write_bytes(b"library")
    (framework / "Versions/3.14/Python").write_bytes(b"python-bytes")
    (framework / "Versions/3.14/Python").chmod(0o755)
    (framework / "Versions/3.14/Resources/Info.plist").write_bytes(b"plist")
    (framework / "Versions/Current").symlink_to("3.14")
    (framework / "Python").symlink_to("Versions/Current/Python")
    (framework / "Resources").symlink_to("Versions/Current/Resources")
    (internal / "Python").symlink_to("Python.framework/Versions/3.14/Python")
    return folder / "open-brain"


def fixture(
    directory: Path,
    platforms: tuple[str, ...],
    *,
    harness: bool = True,
    links: dict[str, str] | None = None,
) -> tuple[Path, Path, Path, Path]:
    folder = directory / "dist/open-brain"
    folder.mkdir(parents=True)
    core, graphify = write_base(folder), directory / "graphify"
    for name, target in (links or {}).items():
        (folder / "_internal" / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / "_internal" / name).symlink_to(target)
    graphify.write_bytes(b"synthetic-graphify")
    archive_digests = {}
    for platform in platforms:
        archive = directory / f"open-brain-0.1.0-{platform}.tar.gz"
        with tarfile.open(archive, "w:gz", format=tarfile.USTAR_FORMAT) as handle:
            handle.add(folder, arcname="open-brain")
        archive_digests[platform] = hashlib.sha256(archive.read_bytes()).hexdigest()
    roles = ("base", "graphify", "harness") if harness else ("base", "graphify")
    artifacts = [
        base_native.ReleaseArtifact(
            version="0.1.0",
            platform_tag=platform,
            role=role,
            sha256=archive_digests[platform] if role == "base" else "a" * 64,
            executable_sha256=hashlib.sha256(
                (core if role == "base" else graphify).read_bytes()
            ).hexdigest(),
            filename="open-brain-"
            + ("" if role == "base" else role + "-")
            + f"0.1.0-{platform}.tar.gz",
            destination=DESTINATIONS[role],
        )
        for platform in platforms
        for role in roles
    ]
    manifest = base_native.write_release_manifest(artifacts, directory / "manifest")
    return core, graphify, manifest, directory / "prepared"


def prepare(paths: tuple[Path, Path, Path, Path], target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(DESKTOP / "scripts/prepare-runtime.mjs"), *map(str, paths), target],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("target", sorted(TARGETS))
@pytest.mark.parametrize("combined", [False, True])
@pytest.mark.parametrize("harness", [False, True])
def test_desktop_selects_exact_pair_from_generated_manifest(
    tmp_path: Path, target: str, combined: bool, harness: bool
) -> None:
    platforms = tuple(sorted(TARGETS.values())) if combined else (TARGETS[target],)
    paths = fixture(tmp_path, platforms, harness=harness)
    completed = prepare(paths, target)
    assert completed.returncode == 0, completed.stderr
    output = paths[3]
    manifest = json.loads((output / "desktop-component-manifest-v1.json").read_bytes())
    assert manifest["target"] == target
    assert manifest["core_version"] == "0.1.0"
    assert [item["role"] for item in manifest["components"]] == ["core", "graphify"]
    assert [item["file"] for item in manifest["components"]] == [
        "runtime/libexec/open-brain/open-brain",
        "runtime/libexec/open-brain-graphify",
    ]
    assert not [path for path in output.rglob("*") if path.is_symlink()]
    copied = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
    base = "runtime/libexec/open-brain/"
    framework = base + "_internal/Python.framework/"
    assert copied == {
        base + "open-brain",
        base + "_internal/base_library.zip",
        base + "_internal/Python",
        framework + "Python",
        framework + "Resources/Info.plist",
        framework + "Versions/3.14/Python",
        framework + "Versions/3.14/Resources/Info.plist",
        framework + "Versions/Current/Python",
        framework + "Versions/Current/Resources/Info.plist",
        "runtime/libexec/open-brain-graphify",
        "desktop-component-manifest-v1.json",
    }
    links = (base + "_internal/Python", framework + "Python", framework + "Versions/Current/Python")
    for link in links:
        assert (output / link).read_bytes() == b"python-bytes"
        assert os.access(output / link, os.X_OK)
    assert not os.access(output / framework / "Resources/Info.plist", os.X_OK)
    for source, item in zip(paths[:2], manifest["components"], strict=True):
        assert (output / item["file"]).read_bytes() == source.read_bytes()
        assert item["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate-base",
        "duplicate-graphify",
        "duplicate-harness",
        "unknown-role",
        "unknown-platform",
        "archive-digest",
        "executable-digest",
        "filename-version",
        "destination",
        "role-filename",
        "malformed-row",
        "noncanonical",
        "mixed-harness",
        "missing-base",
        "missing-graphify",
        "version",
        "non-ascii",
        "blank-row",
    ],
)
def test_desktop_rejects_invalid_authority_rows_even_on_other_platform(
    tmp_path: Path, damage: str
) -> None:
    paths = fixture(tmp_path, tuple(sorted(TARGETS.values())))
    manifest = paths[2]
    rows = manifest.read_text().splitlines()
    fields = rows[2].split(" ")
    if damage.startswith("duplicate-"):
        role = damage.removeprefix("duplicate-")
        rows.append(next(row for row in rows[2:] if row.split(" ")[1] == role))
    elif damage == "malformed-row":
        rows.append("resource harness macos-arm64")
    elif damage == "noncanonical":
        rows[2], rows[3] = rows[3], rows[2]
    elif damage == "mixed-harness":
        rows = [row for row in rows if not row.startswith("resource harness linux-x86_64 ")]
    elif damage.startswith("missing-"):
        role = damage.removeprefix("missing-")
        rows = [row for row in rows if not row.startswith(f"resource {role} macos-arm64 ")]
    elif damage == "version":
        rows[1] = "version ../outside"
    elif damage == "blank-row":
        rows.append("")
    else:
        index, value = {
            "unknown-role": (1, "unknown"),
            "unknown-platform": (2, "unknown-platform"),
            "archive-digest": (3, "g" * 64),
            "executable-digest": (4, "x" * 64),
            "filename-version": (5, "open-brain-9.9.9-linux-x86_64.tar.gz"),
            "destination": (6, "bin/other"),
            "role-filename": (1, "graphify"),
            "non-ascii": (3, "\u00e1" + "a" * 63),
        }[damage]
        fields[index] = value
        rows[2] = " ".join(fields)
    manifest.write_text("\n".join(rows) + "\n")
    with pytest.raises(base_native.BaseNativeError):
        base_native.read_release_manifest(manifest)
    completed = prepare(paths, "aarch64-apple-darwin")
    assert completed.returncode != 0
    assert not paths[3].exists()


@pytest.mark.parametrize(
    ("member", "message"),
    [(0, "does not match the inventory"), (1, "digest does not match")],
)
def test_desktop_rejects_wrong_executable_bytes(tmp_path: Path, member: int, message: str) -> None:
    paths = fixture(tmp_path, ("macos-arm64",))
    paths[member].write_bytes(b"wrong-synthetic-executable")
    completed = prepare(paths, "aarch64-apple-darwin")
    assert completed.returncode != 0
    assert message in completed.stderr
    assert not (paths[3] / "runtime").exists()


@pytest.mark.parametrize(
    "member",
    ["base_library.zip", "Python.framework/Versions/3.14/Python"],
)
def test_desktop_refuses_a_base_folder_that_differs_from_the_release(
    tmp_path: Path, member: str
) -> None:
    paths = fixture(tmp_path, ("macos-arm64",))
    (internal(paths) / member).write_bytes(b"altered after the release archive was built")
    completed = prepare(paths, "aarch64-apple-darwin")
    assert completed.returncode != 0
    assert "does not match the inventory" in completed.stderr
    assert not (paths[3] / "runtime").exists()


def test_desktop_refuses_a_base_archive_that_differs_from_the_manifest(tmp_path: Path) -> None:
    paths = fixture(tmp_path, ("macos-arm64",))
    archive = tmp_path / "open-brain-0.1.0-macos-arm64.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"\0")
    completed = prepare(paths, "aarch64-apple-darwin")
    assert completed.returncode != 0
    assert "base archive digest does not match" in completed.stderr
    assert not paths[3].exists()


def test_desktop_ignores_extra_files_in_the_base_folder(tmp_path: Path) -> None:
    paths = fixture(tmp_path, ("macos-arm64",))
    (internal(paths) / "stray.dylib").write_bytes(b"not in the release")
    completed = prepare(paths, "aarch64-apple-darwin")
    assert completed.returncode == 0, completed.stderr
    assert not (paths[3] / "runtime/libexec/open-brain/_internal/stray.dylib").exists()


def test_desktop_requires_requested_platform_pair(tmp_path: Path) -> None:
    paths = fixture(tmp_path, ("linux-x86_64",))
    completed = prepare(paths, "aarch64-apple-darwin")
    assert completed.returncode != 0
    assert not paths[3].exists()


def internal(paths: tuple[Path, Path, Path, Path]) -> Path:
    return paths[0].parent / "_internal"


@pytest.mark.parametrize(
    "links",
    [
        {"escape": "../../outside"},
        {"absolute": "/etc/hosts"},
        {"backslash": "a\\b"},
        {"dot": "./base_library.zip"},
        {"missing": "no-such-member"},
        {"cycle-a": "cycle-b", "cycle-b": "cycle-a"},
        {"self": "self"},
        {"a/loop": ".."},
        {"outside-internal": "../open-brain"},
        {
            "hop1": "hop2",
            "hop2": "hop3",
            "hop3": "hop4",
            "hop4": "hop5",
            "hop5": "base_library.zip",
        },
    ],
    ids=[
        "escape",
        "absolute",
        "backslash",
        "dot",
        "missing",
        "cycle",
        "self",
        "ancestor",
        "outside-internal",
        "depth-5",
    ],
)
def test_desktop_refuses_unsafe_links(tmp_path: Path, links: dict[str, str]) -> None:
    paths = fixture(tmp_path, ("macos-arm64",), links=links)
    completed = prepare(paths, "aarch64-apple-darwin")
    assert completed.returncode != 0
    assert "base link" in completed.stderr
    assert not paths[3].exists()


def test_desktop_accepts_a_four_hop_link_chain(tmp_path: Path) -> None:
    paths = fixture(
        tmp_path,
        ("macos-arm64",),
        links={"hop1": "hop2", "hop2": "hop3", "hop3": "hop4", "hop4": "base_library.zip"},
    )
    completed = prepare(paths, "aarch64-apple-darwin")
    assert completed.returncode == 0, completed.stderr
    copied = paths[3] / "runtime/libexec/open-brain/_internal/hop1"
    assert copied.read_bytes() == b"library" and not copied.is_symlink()


def test_desktop_replaces_a_stale_runtime_tree(tmp_path: Path) -> None:
    paths = fixture(tmp_path, ("macos-arm64",))
    assert prepare(paths, "aarch64-apple-darwin").returncode == 0
    stray = paths[3] / "runtime/libexec/open-brain/_internal/stray.bin"
    stray.write_bytes(b"left over from an earlier run")
    completed = prepare(paths, "aarch64-apple-darwin")
    assert completed.returncode == 0, completed.stderr
    assert not stray.exists()


VERIFY = """
import {{ readFileSync }} from "node:fs";
const fs = await import("node:fs/promises");
import {{ readArchiveInventory, planFlatten, flattenInto, verifyFlattened }} from "{script}";
const members = readArchiveInventory(readFileSync(process.argv[3]));
const plan = planFlatten(members);
await flattenInto(process.argv[1], plan, process.argv[2]);
{tamper}
await verifyFlattened(plan, process.argv[2]);
"""


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("", None),
        (
            'await fs.writeFile(process.argv[2] + "/_internal/Python", "x");',
            "does not match the inventory",
        ),
        ('await fs.writeFile(process.argv[2] + "/extra", "x");', "extra file"),
        (
            'await fs.rm(process.argv[2] + "/_internal/base_library.zip");',
            "differ from the inventory",
        ),
        ('await fs.symlink("open-brain", process.argv[2] + "/link");', "keeps a symlink"),
    ],
    ids=["clean", "hash-mismatch", "extra-file", "missing-file", "symlink-left"],
)
def test_flattened_output_is_verified_against_the_inventory(
    tmp_path: Path, tamper: str, message: str | None
) -> None:
    paths = fixture(tmp_path, ("macos-arm64",))
    archive = tmp_path / "open-brain-0.1.0-macos-arm64.tar.gz"
    script = VERIFY.format(script=(DESKTOP / "scripts/flatten-base.mjs").as_uri(), tamper=tamper)
    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            str(paths[0].parent),
            str(tmp_path / "out"),
            str(archive),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if message is None:
        assert completed.returncode == 0, completed.stderr
    else:
        assert completed.returncode != 0 and message in completed.stderr
