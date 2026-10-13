"""Desktop selects its pair from the actual native release manifest contract."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tools.open_brain_dev import base_native

DESKTOP = Path(__file__).resolve().parents[1]
TARGETS = {
    "aarch64-apple-darwin": "macos-arm64",
    "x86_64-unknown-linux-gnu": "linux-x86_64",
}
DESTINATIONS = {
    "base": "bin/open-brain",
    "graphify": "libexec/open-brain-graphify",
    "harness": "libexec/open-brain-harness/open-brain-harness",
}


def fixture(
    directory: Path, platforms: tuple[str, ...], *, harness: bool = True
) -> tuple[Path, Path, Path, Path]:
    core, graphify = directory / "core", directory / "graphify"
    core.write_bytes(b"synthetic-core")
    graphify.write_bytes(b"synthetic-graphify")
    roles = ("base", "graphify", "harness") if harness else ("base", "graphify")
    artifacts = [
        base_native.ReleaseArtifact(
            version="0.1.0",
            platform_tag=platform,
            role=role,
            sha256="a" * 64,
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
    copied = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
    assert copied == {
        "runtime/bin/open-brain",
        "runtime/libexec/open-brain-graphify",
        "desktop-component-manifest-v1.json",
    }
    for source, item in zip(paths[:2], manifest["components"], strict=True):
        assert (output / item["file"]).read_bytes() == source.read_bytes()
        assert item["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate-base", "duplicate-graphify", "duplicate-harness", "unknown-role",
        "unknown-platform", "archive-digest", "executable-digest", "filename-version",
        "destination", "role-filename", "malformed-row", "noncanonical", "mixed-harness",
        "missing-base", "missing-graphify", "version", "non-ascii", "blank-row",
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


@pytest.mark.parametrize("member", [0, 1])
def test_desktop_rejects_wrong_executable_bytes(tmp_path: Path, member: int) -> None:
    paths = fixture(tmp_path, ("macos-arm64",))
    paths[member].write_bytes(b"wrong-synthetic-executable")
    completed = prepare(paths, "aarch64-apple-darwin")
    assert completed.returncode != 0
    assert "digest does not match" in completed.stderr


def test_desktop_requires_requested_platform_pair(tmp_path: Path) -> None:
    paths = fixture(tmp_path, ("linux-x86_64",))
    completed = prepare(paths, "aarch64-apple-darwin")
    assert completed.returncode != 0
    assert not paths[3].exists()
